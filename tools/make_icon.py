"""Gera assets/icon.ico (quadrado arredondado em degradê com seta de download)."""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def main() -> None:
    # Degradê horizontal vermelho -> laranja (mesmas cores da interface).
    grad = Image.new("RGBA", (SIZE, SIZE))
    a, b = (255, 61, 87), (255, 122, 69)
    px = grad.load()
    for x in range(SIZE):
        t = x / (SIZE - 1)
        c = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
        for y in range(SIZE):
            px[x, y] = c

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((16, 16, SIZE - 16, SIZE - 16), radius=112, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(grad, mask=mask)

    d = ImageDraw.Draw(icon)
    white = (255, 255, 255, 255)
    cx = SIZE // 2
    d.rounded_rectangle((cx - 34, 110, cx + 34, 290), radius=20, fill=white)  # haste
    d.polygon([(cx - 120, 250), (cx + 120, 250), (cx, 370)], fill=white)       # ponta
    d.rounded_rectangle((130, 392, SIZE - 130, 430), radius=19, fill=white)     # base

    OUT.parent.mkdir(exist_ok=True)
    icon.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.save(OUT.with_suffix(".png"))
    print("ok", OUT)


if __name__ == "__main__":
    main()
