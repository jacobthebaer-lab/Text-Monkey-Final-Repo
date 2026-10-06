"""The coordinator workspace module uses the existing signed-in app session."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()


@router.get('/coordinator-workflows.js', include_in_schema=False)
def workspace_asset():
    return FileResponse(Path(__file__).resolve().parents[2] / 'web/texty/public/coordinator-workflows.js')
