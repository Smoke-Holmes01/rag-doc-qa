import asyncio
import json
import sys

import httpx

from app.config import load_settings
from app.errors import AppError
from app.index import build_index
from app.main import providers


async def run() -> int:
    try:
        settings = load_settings()
        settings.check_live()
        async with httpx.AsyncClient(timeout=settings.timeout(), follow_redirects=False) as client:
            embedding, _ = providers(settings, client)
            index = await build_index(settings, embedding)
        print(json.dumps({"mode": settings.model_mode, "documents": len(index.manifest.documents),
                          "chunks": len(index.chunks), "dimension": index.manifest.dimension,
                          "index_location": str(settings.index_dir / settings.model_mode),
                          "index_fingerprint": index.fingerprint}, ensure_ascii=False))
        return 0
    except AppError as exc:
        print(json.dumps(exc.body("ingest"), ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps(AppError("INDEX_WRITE_FAILED", "索引目录读写失败，请检查权限").body("ingest")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
