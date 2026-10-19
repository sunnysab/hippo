"""Request and response schemas for the HTTP API, one module per domain.

These models are the documented shape of the API: they are attached to the
routers as ``response_model`` or as request bodies, so /docs and the runtime
cannot disagree about a field that is declared here.

Two rules the modules follow:

* A response model names its fields explicitly and stays closed. A credential
  row is one column away from a secret, so passing undeclared keys through is
  only ever allowed for a payload a provider owns (``RowPayload``), never for a
  storage row this API shapes itself: if a row gains a column, the right
  outcome is a field this API chose to publish, not a leak by default.
* A request model declares a field required exactly when the route refuses to
  work without it. Routes keep their own checks for the cases a type cannot
  express (blank text, length, enum), and those checks keep their own messages.
"""
