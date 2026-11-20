"""Private demo evidence: inspect actual images, re-encode and watermark."""
import hashlib
import io
import re
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from werkzeug.exceptions import BadRequest

MAX_FILE = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000


def safe_path(root, name):
    root = Path(root).resolve()
    target = (root / name).resolve()
    if target.parent != root or not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg)', name):
        raise BadRequest('附件存储路径无效')
    return target


def prepare_photos(files):
    files = [f for f in files if f.filename]
    if not 1 <= len(files) <= 3:
        raise BadRequest('本轮必须上传 1–3 张有效照片')
    prepared = []
    for f in files:
        name = f.filename
        # Check the original part header too: multipart option unquoting may
        # consume a Windows backslash before FileStorage.filename is exposed.
        if ('\\' in f.headers.get('Content-Disposition', '') or
                len(name) > 160 or '..' in name or any(c in name for c in '/\\:\x00')
                or Path(name).is_absolute()):
            raise BadRequest('照片文件名不能包含路径或特殊路径字符')
        ext = Path(name).suffix.lower()
        expected = {'.png': ('PNG', 'image/png'), '.jpg': ('JPEG', 'image/jpeg'),
                    '.jpeg': ('JPEG', 'image/jpeg')}.get(ext)
        if not expected or f.mimetype != expected[1]:
            raise BadRequest('只允许扩展名与类型一致的 PNG/JPEG 照片')
        raw = f.stream.read(MAX_FILE + 1)
        if not raw or len(raw) > MAX_FILE:
            raise BadRequest('单张照片不能为空且不能超过 5 MiB')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.format != expected[0] or image.width * image.height > MAX_PIXELS:
                        raise BadRequest('照片内容类型不符或像素超过 2000 万')
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    # Metadata/trailing payload is not copied to the canonical evidence.
                    canvas = Image.new('RGB', (max(image.width, 480), max(image.height, 100)), 'white')
                    canvas.paste(image.convert('RGB'), (0, 0))
                    draw = ImageDraw.Draw(canvas)
                    font_path = Path('C:/Windows/Fonts/msyh.ttc')
                    font = ImageFont.truetype(str(font_path), 20) if font_path.exists() else ImageFont.load_default()
                    label = '演示占位图 / DEMO PLACEHOLDER' if font_path.exists() else 'DEMO PLACEHOLDER'
                    draw.rectangle((0, canvas.height - 35, canvas.width, canvas.height), fill='#162b44')
                    draw.text((8, canvas.height - 30), label, fill='white', font=font)
                    output = io.BytesIO()
                    canvas.save(output, format=expected[0])
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise BadRequest('无法解码或不安全的照片内容') from error
        data = output.getvalue()
        prepared.append(dict(original_name=name, generated_name=uuid.uuid4().hex +
            ('.png' if expected[0] == 'PNG' else '.jpg'), detected_type=expected[1],
            data=data, size=len(data), hash=hashlib.sha256(data).hexdigest()))
    return prepared


def write_photo(path, data):
    with path.open('xb') as stream:
        stream.write(data)
