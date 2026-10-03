"""Public brand assets only; explicit allowlist excludes arbitrary filesystem paths."""
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
BRAND_ROOT = Path(__file__).resolve().parents[2] / "web" / "texty" / "public" / "brand"
BRAND_ASSETS = frozenset({
    'BRAND.md',
    'fonts/Bagel-Fat-One-OFL.txt',
    'fonts/DM-Sans-OFL.txt',
    'fonts/bagel-fat-one.ttf',
    'fonts/dm-sans-400.ttf',
    'fonts/dm-sans-600.ttf',
    'site.webmanifest',
    'textmonkey-app-icon-1024-rounded.png',
    'textmonkey-app-icon-1024-square.png',
    'textmonkey-brand-colors-type.png',
    'textmonkey-favicon-192.png',
    'textmonkey-favicon-512.png',
    'textmonkey-logo-horizontal-dark.png',
    'textmonkey-logo-horizontal-transparent.png',
    'textmonkey-logo-horizontal-yellow.png',
    'textmonkey-logo-stacked-dark.png',
    'textmonkey-logo-stacked-transparent.png',
    'textmonkey-logo-stacked-yellow.png',
    'textmonkey-mark-transparent.png',
    'textmonkey-wordmark-brown-transparent.png',
    'textmonkey-wordmark-white-transparent.png',
    'textmonkey-wordmark-yellow-transparent.png',
})

@router.get("/brand/{asset:path}")
def brand_asset(asset: str):
    if asset not in BRAND_ASSETS:
        raise HTTPException(404)
    return FileResponse(BRAND_ROOT / asset)
