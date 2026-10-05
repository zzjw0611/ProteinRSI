# SPDX-License-Identifier: MIT
from types import SimpleNamespace

import pytest

from proteinrsi.contracts import BudgetSpec, MetaPolicy, TaskSpec, TaskView, Workflow
from proteinrsi.storage import Store
from proteinrsi.tools import ToolGateway
from proteinrsi.dataflow.resources import ResourceStore, standard_registry, scope_for

GB1 = 'MQYKLILNGKTLKGETTTEAVDAATAEKVFKQYANDNGVDGEWTYDDATKTFTVTE'


@pytest.fixture
def view():
    task = TaskSpec(name='GB1 contract fixture', reference_sequence=GB1,
        mutable_positions=[39, 40, 41, 54], max_mutations=4,
        candidate_access='open', controls_per_batch=0, max_rounds=2,
        budget=BudgetSpec(experimental_wells=48, llm_calls=100, tool_calls=100))
    return TaskView(task=task, round_index=0, observations=[], history=[], remaining_wells=48,
                    workflow=Workflow(), meta=MetaPolicy())


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'state')
    value.configure_budget({'experimental_wells': 48, 'llm_calls': 100, 'tool_calls': 100})
    return value


@pytest.fixture
def resources(store, view):
    return ResourceStore(store, standard_registry(), scope_for(view))


@pytest.fixture
def team(store):
    return SimpleNamespace(store=store, tools=ToolGateway(store), llm=None, protein_model=None)
