"""Response builders shared by the API routers."""

from fastapi import Response


def binary_response(payload: bytes, content_type: str) -> Response:
    return Response(
        content=payload,
        media_type=content_type,
        headers={'Cache-Control': 'public, max-age=259200'},
    )
