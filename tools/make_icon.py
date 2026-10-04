"""Dessine l'icône d'Autocut et écrit autocut/assets/autocut.ico et autocut.png.

À relancer seulement si l'icône change. Nécessite PySide6 et Pillow (python -m pip install Pillow).
"""

from io import BytesIO
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF

ASSETS = Path(__file__).resolve().parent.parent / "autocut" / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]
ACCENT = QColor("#ff8a3d")
BARS = [(30, 32), (56, 84), (82, 144), (108, 104), (134, 160), (160, 96), (186, 60), (212, 24)]  # (x, hauteur) sur 256
CUT_TOP, CUT_BOTTOM = QPointF(164, 28), QPointF(102, 228)


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256, size / 256)

    grad = QLinearGradient(0, 0, 0, 256)
    grad.setColorAt(0, QColor("#2a2d36"))
    grad.setColorAt(1, QColor("#17181d"))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)

    bars = QPainterPath()
    for x, h in BARS:
        bars.addRoundedRect(QRectF(x, 128 - h / 2, 16, h), 8, 8)
    # La ligne de coupe sépare la forme d'onde en deux moitiés légèrement décalées
    left = QPainterPath()
    left.addPolygon(QPolygonF([QPointF(0, 0), QPointF(158, 0), QPointF(108, 256), QPointF(0, 256)]))
    right = QPainterPath()
    right.addPolygon(QPolygonF([QPointF(158, 0), QPointF(256, 0), QPointF(256, 256), QPointF(108, 256)]))
    p.setBrush(QColor("#dfe6f0"))
    p.drawPath(bars.intersected(left).translated(-7, 4))
    p.drawPath(bars.intersected(right).translated(7, -4))

    p.setPen(QPen(ACCENT, 12, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(CUT_TOP, CUT_BOTTOM)
    p.end()
    return img


def to_pil(img: QImage) -> Image.Image:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(BytesIO(bytes(data))).convert("RGBA")


def main():
    QGuiApplication([])
    images = [to_pil(draw(s)) for s in SIZES]
    images[-1].save(ASSETS / "autocut.ico", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    images[-1].save(ASSETS / "autocut.png")
    print("Icône écrite dans", ASSETS)


if __name__ == "__main__":
    main()
