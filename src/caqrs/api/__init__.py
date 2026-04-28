"""FastAPI surface for CAQRS — auto-generated OpenAPI documentation
over the existing Pydantic schemas.

Default install does **not** pull FastAPI; this subpackage requires
the ``api`` optional dependency:

.. code-block:: shell

    pip install caqrs[api]
    # or
    uv add --optional api caqrs

Run the dev server with:

.. code-block:: shell

    uvicorn caqrs.api.app:app --reload

Browse:

- ``http://localhost:8000/docs`` — Swagger UI
- ``http://localhost:8000/redoc`` — ReDoc
- ``http://localhost:8000/openapi.json`` — raw OpenAPI 3 spec

The endpoint surface is deliberately small in P3.d: a few introspection
+ projection endpoints that turn the existing typed pipeline into a
documented HTTP surface. The point of this subpackage is **schema
visibility**, not "ship a backend service".
"""

from caqrs.api.app import app, build_app

__all__ = ["app", "build_app"]
