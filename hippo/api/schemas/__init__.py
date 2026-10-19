"""Request and response schemas for the HTTP API, one module per domain.

These models are the documented shape of the API: they are attached to the
routers as ``response_model`` or as request bodies, so /docs and the runtime
cannot disagree about a field that is declared here.

Two rules the modules follow:

* A model fed from a database row or a query helper inherits ``RowPayload``,
  which keeps undeclared keys. A response model that silently truncated a row
  would be a worse bug than a document that names fewer fields.
* A request model declares a field required exactly when the route refuses to
  work without it. Routes keep their own checks for the cases a type cannot
  express (blank text, length, enum), and those checks keep their own messages.
"""
