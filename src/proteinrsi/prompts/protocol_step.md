# Typed research operation

Answer the current step's question using only its supplied input resources and visible
context. Return the exact declared output schema. Data references must refer to actual
supplied resources, and computed evidence is not an experimental measurement.

Do not copy a sequence collection that already exists. Return IDs/references where the
schema asks for them; use an explicit generation operation only for genuinely new data.
Errors identify fields for bounded repair; never relax scientific constraints to make
an output pass. No model runs implicitly. Unknown numerical results must remain unknown.
