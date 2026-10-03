# SPDX-License-Identifier: MIT
"""ESMC-600M via native Transformers; no remote Python, fake weights or silent fallback."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
from typing import Literal

import numpy as np
from pydantic import Field

from proteinrsi.contracts import AMINO_ACIDS, Model, digest
from proteinrsi.storage import Store

MODEL_ID = "biohub/ESMC-600M-hf"
ALPHABET = "ACDEFGHIKLMNPQRSTVWY"


class ESMCConfig(Model):
    model_id: Literal["biohub/ESMC-600M-hf"] = MODEL_ID
    revision: str = Field(default="main", min_length=1)
    device: str = Field(default="cpu", pattern=r"^(cpu|cuda(:[0-9]+)?)$")
    dtype: Literal["float32", "bfloat16"] = "float32"
    batch_size: int = Field(default=2, ge=1, le=32)
    max_residues: int = Field(default=2046, ge=1, le=2046)
    max_masked_positions: int = Field(default=128, ge=1, le=2046)
    max_model_inputs: int = Field(default=4096, ge=1)


def software_versions() -> dict:
    result = {}
    for name in ("torch", "transformers", "huggingface-hub"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not-installed"
    return result


class TransformersBackend:
    """Load the official *-hf conversion with native library code and safetensors only."""

    def __init__(self, store: Store, config: ESMCConfig):
        self.store, self.config = store, config
        self.model = self.tokenizer = None

    def resolve(self, *, download: bool = False) -> tuple[str, str]:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("Install ProteinRSI with pip install -e '.[esmc]'") from exc
        pin = self.store.get("protein_backend", "snapshot")
        revision = pin["revision"] if pin else self.config.revision
        try:
            path = snapshot_download(
                self.config.model_id, revision=revision, local_files_only=not download,
                allow_patterns=["*.json", "*.txt", "*.safetensors", "README.md", "LICENSE*"],
            )
        except Exception as exc:
            raise RuntimeError("ESMC snapshot unavailable. Run esmc-check --download once, "
                               "or prepopulate the Hugging Face cache. No model fallback.") from exc
        resolved = Path(path).name
        if not re.fullmatch(r"[0-9a-f]{40}", resolved):
            raise ValueError("Expected an immutable Hugging Face snapshot commit")
        record = {"model_id": self.config.model_id, "revision": resolved}
        self.store.put("protein_backend", "snapshot", record, immutable=True)
        return path, resolved

    @property
    def identity(self) -> dict:
        _, revision = self.resolve()
        return {"model_id": self.config.model_id, "revision": revision,
                "device": self.config.device, "dtype": self.config.dtype,
                "software": software_versions(), "adapter": "proteinrsi-esmc600m-v1"}

    def load(self, *, download: bool = False) -> None:
        if self.model is not None:
            return
        try:
            import torch
            from transformers import EsmcForMaskedLM, EsmcTokenizer
        except ImportError as exc:
            raise RuntimeError("ESMC needs the esmc extra (native Transformers 5.16.1). "
                               "Do not install fair-esm as a substitute.") from exc
        if self.config.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable; no silent CPU fallback")
        if self.config.dtype == "bfloat16" and (not self.config.device.startswith("cuda")
                                                or not torch.cuda.is_bf16_supported()):
            raise RuntimeError("bfloat16 requires a supported CUDA device; use float32 otherwise")
        path, _ = self.resolve(download=download)
        tokenizer = EsmcTokenizer.from_pretrained(path, local_files_only=True)
        model, info = EsmcForMaskedLM.from_pretrained(
            path, local_files_only=True, use_safetensors=True, output_loading_info=True,
            dtype=getattr(torch, self.config.dtype), attn_implementation="sdpa",
        )
        if any(info.get(k) for k in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
            raise RuntimeError("Checkpoint/library key mismatch; refusing partially random weights")
        if model.config.hidden_size != 1152 or model.config.num_hidden_layers != 36:
            raise ValueError("Expected the 36-layer, 1152-dimensional ESMC-600M checkpoint")
        if tokenizer.padding_side != "right" or tokenizer.mask_token_id is None:
            raise ValueError("Unsupported ESMC tokenizer configuration")
        self.tokenizer = tokenizer
        self.model = model.to(self.config.device).eval()
        self.model.requires_grad_(False)
        self.store.event("protein_model_loaded", self.identity)

    def _inputs(self, sequences: list[str]):
        self.load()
        encoded = self.tokenizer(sequences, padding=True, truncation=False,
                                 return_tensors="pt", return_special_tokens_mask=True)
        special = encoded.pop("special_tokens_mask").bool()
        residues = encoded["attention_mask"].bool() & ~special
        offsets = []
        for i, sequence in enumerate(sequences):
            positions = residues[i].nonzero().flatten()
            ids = encoded["input_ids"][i, positions].tolist()
            if len(ids) != len(sequence) or ids != self.tokenizer.convert_tokens_to_ids(list(sequence)):
                raise ValueError("Token/residue alignment mismatch; never guess a BOS offset")
            offsets.append(positions.tolist())
        if encoded["input_ids"].shape[1] > self.model.config.max_position_embeddings:
            raise ValueError("Sequence exceeds checkpoint context; no silent truncation")
        return {k: v.to(self.config.device) for k, v in encoded.items()}, residues, offsets

    def embed(self, sequences: list[str]) -> list[list[float]]:
        import torch
        result = []
        for start in range(0, len(sequences), self.config.batch_size):
            chunk = sequences[start:start + self.config.batch_size]
            inputs, residues, _ = self._inputs(chunk)
            with torch.inference_mode():
                hidden = self.model.base_model(**inputs, return_dict=True).last_hidden_state.float()
                mask = residues.to(hidden.device).unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1)
            result.extend(pooled.cpu().tolist())
        return result

    def masked(self, reference: str, positions: list[int]) -> list[list[float]]:
        import torch
        result = []
        for start in range(0, len(positions), self.config.batch_size):
            chunk = positions[start:start + self.config.batch_size]
            inputs, _, offsets = self._inputs([reference] * len(chunk))
            indices = [offsets[i][position - 1] for i, position in enumerate(chunk)]
            for i, index in enumerate(indices):
                inputs["input_ids"][i, index] = self.tokenizer.mask_token_id
            with torch.inference_mode():
                logits = self.model(**inputs, return_dict=True).logits.float()
                rows = logits[torch.arange(len(chunk), device=logits.device), indices]
                logp = rows.log_softmax(-1)
                aa_ids = self.tokenizer.convert_tokens_to_ids(list(ALPHABET))
                result.extend(logp[:, aa_ids].cpu().tolist())
        return result


class ESMC600M:
    """Persistent per-sequence/per-mask cache, with budgeted actual model inputs."""

    def __init__(self, store: Store, config: ESMCConfig, *, backend=None):
        self.store, self.config = store, config
        self.backend = backend or TransformersBackend(store, config)

    @property
    def identity(self) -> dict:
        return {**self.backend.identity, "configuration": self.config.model_dump()}

    def validate(self, sequence: str) -> None:
        if not sequence or set(sequence) - AMINO_ACIDS:
            raise ValueError("ESMC requires uppercase canonical single-chain protein sequences")
        if len(sequence) > self.config.max_residues:
            raise ValueError("ESMC sequence length limit exceeded; no silent truncation")

    def _cached(self, kind: str, items: list, compute, width: int) -> list[list[float]]:
        identity = self.identity
        keys = [digest({"model": identity, "kind": kind, "item": item}) for item in items]
        unique = dict(zip(keys, items))
        missing = {key: item for key, item in unique.items() if self.store.get("plm_cache", key) is None}
        if missing:
            job = "plm-" + digest({"kind": kind, "keys": list(missing)})
            if self.store.get("plm_jobs", job):
                raise RuntimeError("Prior ESMC call failed/uncertain; inspect audit, do not resubmit blindly")
            self.store.reserve(job, "plm_inputs", len(missing), list(missing))
            self.store.settle(job)
            self.store.put("plm_jobs", job, {"state": "started"})
            try:
                rows = np.asarray(compute(list(missing.values())), dtype=float)
                if rows.shape != (len(missing), width) or not np.isfinite(rows).all():
                    raise ValueError("Invalid ESMC output shape or nonfinite values")
                for key, row in zip(missing, rows):
                    self.store.put("plm_cache", key, row.tolist(), immutable=True)
                self.store.put("plm_jobs", job, {"state": "done", "inputs": len(missing)})
                self.store.event("plm_completed", {"job": job, "kind": kind, "inputs": len(missing),
                                                    "model": identity})
            except Exception as exc:
                self.store.put("plm_jobs", job, {"state": "failed", "error_type": type(exc).__name__})
                raise
        return [self.store.get("plm_cache", key) for key in keys]

    def embed(self, sequences: list[str]) -> np.ndarray:
        if not sequences:
            return np.empty((0, 1152))
        for sequence in sequences:
            self.validate(sequence)
        return np.asarray(self._cached("residue_mean_embedding", sequences, self.backend.embed, 1152))

    def masked(self, reference: str, positions: list[int]) -> dict[int, np.ndarray]:
        self.validate(reference)
        if any(type(p) is not int or not 1 <= p <= len(reference) for p in positions):
            raise ValueError("Positions must be 1-based residue indices")
        positions = sorted(set(positions))
        if len(positions) > self.config.max_masked_positions:
            raise ValueError("Too many masked positions for one request; narrow the design region")
        if not positions:
            return {}
        items = [[reference, p] for p in positions]
        rows = self._cached("masked_log_probabilities", items,
                            lambda xs: self.backend.masked(reference, [x[1] for x in xs]), 20)
        return {p: np.asarray(row) for p, row in zip(positions, rows)}

    def score_variants(self, reference: str, sequences: list[str]) -> list[float]:
        self.validate(reference)
        changed = []
        for sequence in sequences:
            self.validate(sequence)
            if len(sequence) != len(reference):
                raise ValueError("Masked-marginal scores support substitutions, not indels")
            changed.append([i + 1 for i, (a, b) in enumerate(zip(reference, sequence)) if a != b])
        logp = self.masked(reference, sorted({p for ps in changed for p in ps}))
        return [float(sum(logp[p][ALPHABET.index(sequence[p - 1])]
                          - logp[p][ALPHABET.index(reference[p - 1])] for p in ps))
                for sequence, ps in zip(sequences, changed)]

    def suggest(self, reference: str, positions: list[int], top_k: int) -> list[dict]:
        if type(top_k) is not int or not 1 <= top_k <= 384:
            raise ValueError("top_k must be between 1 and 384")
        rows = []
        for p, logp in self.masked(reference, positions).items():
            for i, aa in enumerate(ALPHABET):
                if aa != reference[p - 1]:
                    score = float(logp[i] - logp[ALPHABET.index(reference[p - 1])])
                    rows.append((score, {"sequence": reference[:p - 1] + aa + reference[p:],
                                 "source": "esmc600m_masked_marginal", "evidence_kind": "proxy",
                                 "rationale": f"Sequence prior log-odds={score:.6g}; NOT measured fitness."}))
        return [row for _, row in sorted(rows, key=lambda pair: (-pair[0], pair[1]["sequence"]))[:top_k]]


def from_store(store: Store) -> ESMC600M | None:
    raw = store.get("configuration", "protein_model")
    return ESMC600M(store, ESMCConfig.model_validate(raw)) if raw else None
