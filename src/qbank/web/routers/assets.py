"""图片上传与读取，以及 Markdown 实时预览。

这两件事放一起，因为它们共同支撑编辑器：粘贴图片得到 ``asset://`` 引用，
预览接口再把这个引用解析成 ``/assets/<sha>`` 并渲染出来。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from qbank.app import AppContext
from qbank.core.content.render import ASSET_SCHEME, MathMode, RenderOptions, render_markdown
from qbank.core.services.asset import (
    SUPPORTED_IMAGE_TYPES,
    AssetService,
    UnsupportedAssetError,
)
from qbank.web.deps import get_context, get_session

router = APIRouter()

#: 单张图片的上限。界面上传比导入更随意（直接粘手机照片），需要一道闸。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def asset_url(sha256: str) -> str:
    return f"/assets/{sha256}"


def render_for_web(markdown: str) -> str:
    """渲染题目内容供页面显示。

    公式用 MathML 而不是 KaTeX：服务端转换，浏览器原生渲染，不需要任何 JS 与
    字体文件，界面完全离线可用。
    """
    return render_markdown(
        markdown,
        RenderOptions(math=MathMode.MATHML, asset_resolver=asset_url),
    )


@router.post("/preview", response_class=HTMLResponse)
def preview(text: str = Form("")) -> HTMLResponse:
    """实时预览。编辑器每次输入（防抖后）调一次。

    不接触数据库，所以不挂 session 依赖——预览是纯函数，让它尽量轻。
    """
    if not text.strip():
        return HTMLResponse('<p class="placeholder">在左侧输入内容，这里会显示渲染效果。</p>')
    return HTMLResponse(render_for_web(text))


@router.post("/assets")
async def upload_asset(
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
) -> JSONResponse:
    """接收一张图片，返回它的 ``asset://`` 引用。

    前端把这个引用插进 Markdown 正文。此时图片还没有被任何题目引用
    （``ref_count`` 为 0），保存题目时才建立关联——所以半途放弃编辑产生的
    图片会变成孤儿，由 ``qbank asset clean`` 或界面上的清理入口回收。
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"图片超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB，请先压缩",
        )
    context: AppContext = get_context(request)
    try:
        asset, _ = AssetService(session, context.paths).store_bytes(
            data, original_name=file.filename or ""
        )
    except UnsupportedAssetError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ref": f"{ASSET_SCHEME}{asset.sha256}",
            "url": asset_url(asset.sha256),
            "name": asset.original_name or "图",
            "width": asset.width,
            "height": asset.height,
        }
    )


@router.get("/assets/{sha256}")
def read_asset(
    sha256: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    """按内容哈希读图。

    哈希即内容，所以可以放心长期缓存——同一个 URL 的内容永远不会变。
    """
    if not sha256.isalnum():
        raise HTTPException(status_code=400, detail="非法的图片标识")
    context: AppContext = get_context(request)
    assets = AssetService(session, context.paths)
    path = assets.path_of(sha256)
    if path is None:
        raise HTTPException(status_code=404, detail="图片不存在或文件已丢失")
    asset = assets.get(sha256)
    media_type = asset.mime if asset else "application/octet-stream"
    if media_type not in SUPPORTED_IMAGE_TYPES:
        media_type = "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
