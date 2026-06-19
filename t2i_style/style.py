from PIL import Image, ImageDraw, ImageFilter


def modern_clear_background(width: int, height: int) -> Image.Image:
    scale = 2
    canvas = Image.new("RGB", (width * scale, height * scale), (222, 216, 202))
    draw = ImageDraw.Draw(canvas)

    top = (229, 224, 211)
    bottom = (207, 200, 184)
    for y in range(height * scale):
        ratio = y / max(height * scale - 1, 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line((0, y, width * scale, y), fill=color)

    margin = 28 * scale
    radius = 18 * scale
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_box = (
        margin + 10 * scale,
        margin + 14 * scale,
        width * scale - margin + 10 * scale,
        height * scale - margin + 14 * scale,
    )
    shadow_draw.rounded_rectangle(shadow_box, radius=radius, fill=(80, 67, 48, 38))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * scale))

    paper = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    paper_draw = ImageDraw.Draw(paper)
    paper_box = (margin, margin, width * scale - margin, height * scale - margin)
    paper_draw.rounded_rectangle(
        paper_box,
        radius=radius,
        fill=(255, 252, 238, 255),
        outline=(205, 191, 164, 255),
        width=2 * scale,
    )

    left, top_y, right, bottom_y = paper_box
    line_gap = 42 * scale
    first_line = top_y + 76 * scale
    for y in range(first_line, bottom_y - 18 * scale, line_gap):
        paper_draw.line((left + 34 * scale, y, right - 28 * scale, y), fill=(126, 170, 205, 72), width=1 * scale)

    margin_line_x = left + 72 * scale
    paper_draw.line((margin_line_x, top_y + 10 * scale, margin_line_x, bottom_y - 10 * scale), fill=(210, 78, 88, 120), width=2 * scale)
    paper_draw.line((margin_line_x + 8 * scale, top_y + 10 * scale, margin_line_x + 8 * scale, bottom_y - 10 * scale), fill=(210, 78, 88, 45), width=1 * scale)

    hole_x = left + 28 * scale
    hole_radius = 9 * scale
    hole_shadow = 2 * scale
    hole_step = 78 * scale
    y = top_y + 70 * scale
    while y < bottom_y - 42 * scale:
        paper_draw.ellipse(
            (hole_x - hole_radius + hole_shadow, y - hole_radius + hole_shadow, hole_x + hole_radius + hole_shadow, y + hole_radius + hole_shadow),
            fill=(108, 91, 64, 42),
        )
        paper_draw.ellipse(
            (hole_x - hole_radius, y - hole_radius, hole_x + hole_radius, y + hole_radius),
            fill=(224, 216, 195, 255),
            outline=(188, 174, 146, 255),
            width=1 * scale,
        )
        paper_draw.arc(
            (hole_x - 18 * scale, y - 18 * scale, hole_x + 36 * scale, y + 18 * scale),
            start=88,
            end=272,
            fill=(115, 105, 92, 150),
            width=3 * scale,
        )
        y += hole_step

    paper_draw.rectangle((left, top_y + 1 * scale, right, top_y + 11 * scale), fill=(255, 255, 255, 52))

    composed = Image.alpha_composite(canvas.convert("RGBA"), shadow)
    composed = Image.alpha_composite(composed, paper)
    return composed.resize((width, height), Image.Resampling.LANCZOS)
