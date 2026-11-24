"""Drawing helpers for the table, the balls, the pockets and the cue.

Every function returns Pillow ``RGBA`` images that are blitted as per-pixel
alpha layered windows by the shell.
"""
import math

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _credit_font(size):
    try:
        return ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', size)
    except OSError:
        return ImageFont.load_default(size)


def round_actions_layout(table):
    """Button drawing and hit testing share the same desktop-client rectangles."""
    size = max(12, min(22, round(table.height / 40)))
    width, height, gap = size * 8, size * 3, size
    left, top, right, bottom = table.bounds
    x = round((left + right - width * 2 - gap) / 2)
    y = round((top + bottom - height) / 2)
    return size, (
        ('again', 'Play Again', (x, y, x + width, y + height)),
        ('quit', 'Quit Game', (x + width + gap, y, x + width * 2 + gap, y + height)),
    )


def round_actions_art(table, hovered=None, pressed=None):
    size, rows = round_actions_layout(table)
    ox, oy = rows[0][2][:2]
    frame = Image.new('RGBA', (rows[-1][2][2] - ox, rows[-1][2][3] - oy))
    draw = ImageDraw.Draw(frame)
    font = _credit_font(size)
    for action, text, (left, top, right, bottom) in rows:
        if action == 'again':
            fill = (105, 221, 179) if hovered != action else (134, 239, 199)
            color = (12, 39, 31)
        else:
            fill = (25, 49, 43) if hovered != action else (40, 71, 62)
            color = (231, 248, 240)
        if pressed == action:
            fill = tuple(round(channel * .85) for channel in fill)
        draw.rounded_rectangle((left - ox, top - oy, right - ox - 1, bottom - oy - 1),
                               radius=size // 2, fill=(*fill, 248), outline=(107, 165, 143, 230))
        draw.text(((left + right) / 2 - ox, (top + bottom) / 2 - oy), text,
                  font=font, anchor='mm', fill=color)
    return frame, ox, oy


def credit_layout(table, credit):
    """Share the text positions and hit rectangles in desktop coordinates."""
    left, top, right, bottom = table.bounds
    font = _credit_font(round(max(11, table.height / 66)))
    margin = table.work_area[3] - bottom if table.desktop else 0
    lines = credit.splitlines()
    cx = left + table.width * .75
    cy = bottom + max(14, margin * .46)
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    boxes = [draw.textbbox((0, 0), line, font=font, anchor='mm', stroke_width=1) for line in lines]
    heights = [box[3] - box[1] + 6 for box in boxes]
    total = sum(heights) + 2 * (len(lines) - 1)
    cursor = bottom + (margin - total) / 2
    rows = []
    for index, line in enumerate(lines):
        y = cy if len(lines) == 1 else cursor + 3 - boxes[index][1]
        rect = draw.textbbox((cx, y), line, font=font, anchor='mm', stroke_width=1)
        rows.append((line, cx, y, (rect[0] - 3, rect[1] - 3, rect[2] + 3, rect[3] + 3)))
        cursor += heights[index] + 2
    return font, rows


def table_art(table, wallpaper=None, credit=None, pockets=True):
    """Draw the table frame and pockets; returns ``(frame, origin_x, origin_y)``.

    ``credit`` is a short source line drawn faintly in the margin below the table, between two
    pockets, so recordings of the game keep their origin.
    """
    left, top, right, bottom = table.bounds
    width = right - left
    height = bottom - top
    pad = math.ceil(table.pocket_radius) + 5
    origin_x, origin_y = left - pad, top - pad
    minimal = table.desktop and table.manual
    if table.desktop:
        origin_x, origin_y, outer_right, outer_bottom = table.work_area
        frame = Image.new('RGBA', (outer_right - origin_x, outer_bottom - origin_y),
                          (8, 26, 25, 40 if minimal else 57))
    else:
        frame = Image.new('RGBA', (width + 2 * pad + 1, height + 2 * pad + 1))
    draw = ImageDraw.Draw(frame)
    rail = max(9, min(22, pad // 2))
    x0 = left - origin_x
    y0 = top - origin_y
    if not minimal:
        draw.rounded_rectangle((x0 - rail, y0 - rail, x0 + width + rail, y0 + height + rail), radius=rail * 2,
                               fill=(75, 51, 38, 178 if table.desktop else 238),
                               outline=(202, 149, 91, 200 if table.desktop else 244), width=3)
    felt = (0, 0, 0, 0) if table.desktop else (14, 82, 63, 156)
    if wallpaper is not None:
        blur = wallpaper.filter(ImageFilter.GaussianBlur(radius=max(10, height / 55)))
        frame.paste(blur.crop((x0, y0, x0 + width + 1, y0 + height + 1)), (x0, y0))
        felt = None
    draw.rectangle((x0, y0, x0 + width, y0 + height), fill=felt,
                   outline=(105, 221, 179, 184) if minimal else (61, 155, 120, 168 if table.desktop else 238),
                   width=3 if minimal else 5)
    # Leave the pocket openings clear so the rail never crosses a basket's mouth.
    for px, py in table.pockets:
        x, y, r = px - origin_x, py - origin_y, table.pocket_radius
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(0, 0, 0, 0))
    if pockets:
        r = round(table.pocket_radius)
        for px, py in table.pockets:
            x, y = px - origin_x, py - origin_y
            draw.ellipse((x - r, y - r, x + r, y + r),
                         fill=(11, 22, 21, 100 if minimal else 248),
                         outline=(105, 221, 179, 105) if minimal else (153, 116, 77, 255),
                         width=2 if minimal else 3)
            inset = r * .23
            draw.ellipse((x - r + inset, y - r + inset, x + r - inset, y + r - inset),
                         fill=(4, 10, 11, 120 if minimal else 255))
    if credit:
        font, rows = credit_layout(table, credit)
        for line, cx, cy, _ in rows:
            draw.text((cx - origin_x, cy - origin_y), line, font=font, anchor='mm',
                      fill=(255, 255, 255, 140), stroke_width=1, stroke_fill=(0, 0, 0, 90))
    return frame, origin_x, origin_y


def pocket_art(table, source):
    """Place the native Shell recycle-bin artwork at the six pocket centres."""
    pad = max(math.ceil(table.pocket_radius), math.ceil(max(source.size) / 2)) + 5
    left, top, right, bottom = table.bounds
    ox, oy = left - pad, top - pad
    frame = Image.new('RGBA', (right - left + 2 * pad + 1, bottom - top + 2 * pad + 1))
    bounds = source.getchannel('A').getbbox()
    if not bounds:
        raise RuntimeError('Windows returned an empty recycle bin icon')
    # Keep the Shell image's native desktop size and transparent margins.
    icon = source
    for px, py in table.pockets:
        frame.alpha_composite(icon, (round(px - ox - icon.width / 2),
                                     round(py - oy - icon.height / 2)))
    return frame, ox, oy



def charge_art(percent, font, falling=False):
    """Compact power meter with a full track, percentage and cycle direction."""
    percent = max(0, min(100, percent))
    pad = max(4, font.size // 2)
    width, height = font.size * 10, font.size * 3
    frame = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=pad,
                           fill=(13, 25, 23, 244), outline=(74, 105, 94, 220))
    label_y = pad + font.size // 2
    draw.text((pad, label_y), 'Power', font=font, anchor='lm', fill=(222, 238, 231))
    draw.text((width - pad, label_y), f'{percent}%', font=font, anchor='rm', fill=(133, 242, 198))
    # A drawn chevron avoids depending on arrow glyphs in the selected font.
    cx, cy = width // 2, label_y
    direction = 1 if falling else -1
    span = max(2, font.size // 5)
    draw.line(((cx - span, cy - direction * span), (cx, cy + direction * span),
               (cx + span, cy - direction * span)), fill=(133, 242, 198), width=2)
    left, right = pad, width - pad - 1
    bottom = height - pad - 1
    top = bottom - pad + 1
    draw.rounded_rectangle((left, top, right, bottom), radius=pad // 2,
                           fill=(49, 76, 70), outline=(71, 99, 89))
    fill_width = round((right - left + 1) * percent / 100)
    if fill_width:
        draw.rounded_rectangle((left, top, left + fill_width - 1, bottom),
                               radius=min(pad, fill_width) // 2, fill=(111, 239, 185))
    return frame


# Colour choices for the commentary bubble live with the caller: the overlay reads wallpaper
# luma and passes either palette in. This module only measures, wraps and paints.
COMMENTARY_DARK = {
    'background': (12, 25, 23, 238),
    'border': (105, 221, 179, 220),
    'text': (231, 248, 240, 255),
}
COMMENTARY_LIGHT = {
    'background': (245, 248, 246, 238),
    'border': (45, 86, 73, 220),
    'text': (18, 38, 33, 255),
}
COMMENTARY_FADE_IN = .18
COMMENTARY_FADE_OUT = .30
COMMENTARY_HOLD_MIN = 3.8
COMMENTARY_HOLD_MAX = 4.5
COMMENTARY_BUBBLE_WIDTH = 360


def commentary_timing(text):
    """Return ``(fade_in, hold, fade_out)`` seconds for one commentary line.

    Hold grows from 3.8 s to 4.5 s as the line gets longer (16 to 48 characters).
    """
    length = len(str(text or '').strip())
    share = max(0.0, min(1.0, (length - 16) / 32.0))
    hold = COMMENTARY_HOLD_MIN + (COMMENTARY_HOLD_MAX - COMMENTARY_HOLD_MIN) * share
    return COMMENTARY_FADE_IN, hold, COMMENTARY_FADE_OUT


def commentary_fade(progress, text=''):
    """Return the 0..1 opacity multiplier at ``progress`` seconds after ``text`` appeared."""
    try:
        elapsed = max(0.0, float(progress))
    except (TypeError, ValueError):
        elapsed = 0.0
    fade_in, hold, fade_out = commentary_timing(text)
    total = fade_in + hold + fade_out
    if elapsed <= 0 or elapsed >= total:
        return 0.0
    if elapsed < fade_in:
        return elapsed / fade_in
    if elapsed < fade_in + hold:
        return 1.0
    return max(0.0, (total - elapsed) / fade_out)


def _commentary_color(palette, *names):
    """Read one RGB/RGBA colour from a caller palette, allowing a few key aliases."""
    try:
        for name in names:
            if name not in palette:
                continue
            channels = tuple(round(float(value)) for value in palette[name][:4])
            if len(channels) < 3:
                continue
            if len(channels) == 3:
                channels += (255,)
            return tuple(max(0, min(255, channel)) for channel in channels)
    except (TypeError, ValueError) as exc:
        raise TypeError(f'invalid commentary palette colour: {names[0]}') from exc
    raise KeyError(f'commentary palette needs one of: {", ".join(names)}')


def _commentary_text_width(draw, text, font):
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font, anchor='mm', stroke_width=1)
    return box[2] - box[0]


def _commentary_ellipsize(draw, text, font, limit):
    if _commentary_text_width(draw, text, font) <= limit:
        return text
    ellipsis = '…'
    characters = list(text)
    while characters and _commentary_text_width(draw, ''.join(characters) + ellipsis, font) > limit:
        characters.pop()
    return ''.join(characters).rstrip() + ellipsis


def commentary_wrap(text, *, width=COMMENTARY_BUBBLE_WIDTH, font=None, padding=14):
    """Wrap ``text`` into at most two measured lines, ellipsising any overflow.

    ``font`` defaults to the Vietnamese-capable credit font. Both this helper and
    :func:`commentary_art` size text with ``textbbox`` so the caller's font is honoured.
    """
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    font = font if font is not None else _credit_font(18)
    limit = max(80, round(width) - 2 * round(padding))
    words = str(text or '').split()
    if not words:
        return ('',)
    lines = ['']
    for word in words:
        candidate = f'{lines[-1]} {word}'.strip()
        if _commentary_text_width(draw, candidate, font) <= limit:
            lines[-1] = candidate
        elif len(lines) < 2:
            lines.append(word)
        else:
            lines[-1] = _commentary_ellipsize(draw, candidate, font, limit)
            break
    return tuple(_commentary_ellipsize(draw, line, font, limit) for line in lines[:2])


def commentary_art(text, *, palette, progress=0.0, fade=None,
                   width=COMMENTARY_BUBBLE_WIDTH, font=None, anchor=None, padding=14):
    """Draw a commentary bubble as ``(RGBA frame, offset_x, offset_y)``.

    ``palette`` is the only colour source: it needs ``background``, ``border`` and
    ``text`` RGB/RGBA entries (``bubble``/``fill``/``bg``, ``outline``/``edge`` and
    ``foreground``/``ink``/``fg`` are aliases). The overlay selects a dark or light
    palette from wallpaper luma; no luma logic runs here. ``COMMENTARY_DARK`` and
    ``COMMENTARY_LIGHT`` are ready-made caller palettes.

    ``progress`` is seconds since the line appeared and drives the default envelope
    (180 ms fade in, 3.8-4.5 s hold by text length, 300 ms fade out). Pass ``fade``
    (0..1) to override that envelope with a caller-computed multiplier.
    ``width`` defaults to 360 px; lines are wrapped with ``textbbox`` and clipped to
    at most two lines with an ellipsis. ``font`` defaults to the Vietnamese-capable
    credit font. Blit the returned frame at ``(offset_x, offset_y)`` in the caller's
    coordinate space; ``anchor=(x, y)`` centres the bubble on that point.
    """
    text = text if isinstance(text, str) else ''
    font = font if font is not None else _credit_font(18)
    bubble_width = max(80, round(width))
    pad = max(4, round(padding))
    lines = commentary_wrap(text, width=bubble_width, font=font, padding=pad)
    line_height = round(getattr(font, 'size', 18) or 18) + 6
    gap = 3
    bubble_height = 2 * pad + len(lines) * line_height + (len(lines) - 1) * gap
    background = _commentary_color(palette, 'background', 'bubble', 'fill', 'bg')
    border = _commentary_color(palette, 'border', 'outline', 'edge')
    ink = _commentary_color(palette, 'text', 'foreground', 'ink', 'fg')
    if fade is None:
        multiplier = commentary_fade(progress, text)
    else:
        multiplier = max(0.0, min(1.0, float(fade)))

    def faded(color):
        return (*color[:3], round(color[3] * multiplier))

    frame = Image.new('RGBA', (bubble_width, bubble_height))
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((0, 0, bubble_width - 1, bubble_height - 1), radius=pad,
                           fill=faded(background), outline=faded(border), width=2)
    for index, line in enumerate(lines):
        if not line:
            continue
        y = pad + line_height / 2 + index * (line_height + gap)
        draw.text((bubble_width / 2, y), line, font=font, anchor='mm', fill=faded(ink),
                  stroke_width=1, stroke_fill=faded(background))
    if anchor is None:
        return frame, 0, 0
    return frame, round(anchor[0] - bubble_width / 2), round(anchor[1] - bubble_height / 2)


# Bright strokes with a dark casing on dark felt, deep strokes with a white casing on bright
# wallpapers. The casing keeps the dashes readable whatever the background does.
GUIDE_DARK = {'incoming': (240, 252, 247, 245), 'white': (150, 205, 255, 250), 'target': (108, 245, 183, 250),
              'casing': (4, 14, 13, 190)}
GUIDE_LIGHT = {'incoming': (26, 42, 48, 245), 'white': (10, 70, 168, 250), 'target': (0, 118, 68, 250),
               'casing': (255, 255, 255, 215)}
BRIGHT_BACKGROUND = 150
GUIDE_SCALE = 2
DASH, GAP = 12, 8


def guide_palette(luma, start, end):
    """Pick the light or dark palette from the wallpaper brightness along one guide line."""
    if luma is None:
        return GUIDE_DARK
    samples = [luma(start[0] + (end[0] - start[0]) * t / 4, start[1] + (end[1] - start[1]) * t / 4)
               for t in range(5)]
    samples = [value for value in samples if value is not None]
    if samples and sum(samples) / len(samples) > BRIGHT_BACKGROUND:
        return GUIDE_LIGHT
    return GUIDE_DARK


def guide_art(lines, circles=(), luma=None):
    """Draw the aiming preview; returns ``(frame, origin_x, origin_y)``.

    ``lines`` are ``(role, start, end)`` dashed paths. ``circles`` are ``(role, centre, radius)``
    collision circles, drawn faintly: ``ghost`` is the cue ball at the moment of contact and
    ``target`` the ball it will hit. ``luma(x, y)`` returns the wallpaper brightness (0-255)
    under a point, or ``None``, and selects a palette that contrasts with it.
    """
    xs = [point[0] for _, start, end in lines for point in (start, end)]
    ys = [point[1] for _, start, end in lines for point in (start, end)]
    for _, (cx, cy), radius in circles:
        xs += [cx - radius, cx + radius]
        ys += [cy - radius, cy + radius]
    pad = 9
    left, top = min(xs) - pad, min(ys) - pad
    width = math.ceil((max(xs) - left + pad) / 64) * 64
    height = math.ceil((max(ys) - top + pad) / 64) * 64
    s = GUIDE_SCALE  # draw enlarged and shrink back: Pillow lines have no anti-aliasing
    frame = Image.new('RGBA', (width * s, height * s))
    draw = ImageDraw.Draw(frame)
    for role, (cx, cy), radius in circles:
        palette = guide_palette(luma, (cx, cy), (cx, cy))
        box = ((cx - left - radius) * s, (cy - top - radius) * s, (cx - left + radius) * s, (cy - top + radius) * s)
        core = palette['white' if role == 'ghost' else 'target']
        if role == 'target':
            draw.ellipse(box, fill=core[:3] + (26,))
        draw.ellipse(box, outline=palette['casing'][:3] + (110,), width=round(4.5 * s))
        draw.ellipse(box, outline=core[:3] + (150 if role == 'ghost' else 125,), width=round(1.8 * s))
    strokes = []
    for role, start, end in lines:
        length = math.dist(start, end)
        if length < 1:
            continue
        palette = guide_palette(luma, start, end)
        x, y = (start[0] - left) * s, (start[1] - top) * s
        dx, dy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
        usable = max(0., length - 5)
        dashes = []
        offset = 0.
        while offset < max(1., usable):
            stop = min(usable, offset + DASH)
            dashes.append(((x + dx * offset * s, y + dy * offset * s), (x + dx * stop * s, y + dy * stop * s)))
            offset += DASH + GAP
        strokes.append((palette, palette[role], dashes, ((end[0] - left) * s, (end[1] - top) * s)))
    # Casings first, then the strokes, so crossing lines never cover each other's outline.
    for palette, _, dashes, _ in strokes:
        for a, b in dashes:
            draw.line((a, b), fill=palette['casing'], width=round(5.5 * s))
    for palette, color, dashes, tip in strokes:
        for a, b in dashes:
            draw.line((a, b), fill=color, width=round(2.6 * s))
        r = 5 * s
        draw.ellipse((tip[0] - r - s, tip[1] - r - s, tip[0] + r + s, tip[1] + r + s),
                     outline=palette['casing'], width=round(2 * s))
        draw.ellipse((tip[0] - r, tip[1] - r, tip[0] + r, tip[1] + r), outline=color, width=round(2.2 * s))
    return frame.reduce(s), left, top


def white_art(radius, shaded=False, texture=None):
    """Draw the cue ball, optionally shaded as a lit sphere."""
    side = max(3, math.ceil(radius * 2) + 4)
    frame = Image.new('RGBA', (side * 3, side * 3))
    if shaded or texture is not None:
        texture_pixels = texture.load() if texture is not None else None
        center = (side * 3 - 1) / 2
        r = max(1, radius * 3)
        pixels = []
        for py in range(side * 3):
            y = (py - center) / r
            for px in range(side * 3):
                x = (px - center) / r
                z2 = 1 - x * x - y * y
                if z2 < 0:
                    pixels.append((0, 0, 0, 0))
                    continue
                z = math.sqrt(z2)
                light = max(0, -.46 * x - .49 * y + .74 * z)
                shine = math.exp(-((x + .34) ** 2 + (y + .40) ** 2) / .038)
                value = min(255, round(157 + 96 * light + 32 * shine))
                if texture_pixels is None:
                    pixels.append((value, min(255, value + 4), min(255, value + 9), 255))
                else:
                    tx = round((x + 1) * .5 * (texture.width - 1))
                    ty = round((y + 1) * .5 * (texture.height - 1))
                    color = texture_pixels[tx, ty]
                    factor = .78 + .22 * light
                    pixels.append((*[min(255, round(c * factor + 20 * shine)) for c in color[:3]], 255))
        frame.putdata(pixels)
        return frame.resize((side, side), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(frame)
    draw.ellipse((5, 5, side * 3 - 6, side * 3 - 6), fill=(246, 249, 250, 255), outline=(134, 155, 169, 255), width=3)
    draw.ellipse((side * .65, side * .55, side * 1.15, side * 1.05), fill=(255, 255, 255, 255))
    return frame.resize((side, side), Image.Resampling.LANCZOS)


def pocket_particles(frame, index, fade):
    """Draw the ripple and the few sparks that accompany an icon sinking into a pocket."""
    frame = frame.copy()
    draw = ImageDraw.Draw(frame)
    center = frame.width / 2
    glow = math.sin(math.pi * fade) ** .65  # 0 at both ends, strongest mid-way
    radius = 10 + fade * 32
    draw.ellipse((center - radius, center - radius, center + radius, center + radius),
                 outline=(173, 255, 221, round(150 * glow)), width=2)
    for i in range(6):
        angle = i * math.tau / 6 + index * .61
        distance = fade * (12 + i % 3 * 7)
        x = center + math.cos(angle) * distance
        y = center + math.sin(angle) * distance
        size = max(.7, (1 - fade) * (2 + i % 2))
        draw.ellipse((x - size, y - size, x + size, y + size), fill=(173, 255, 221, round(210 * glow)))
    return frame


def cue_art(cue, thickness=1):
    """Draw a cue from ``(tip, tail)``; returns ``(frame, offset_x, offset_y)``."""
    tip, tail = cue
    side = math.ceil(math.dist(tip, tail)) + 18
    x = round((tip[0] + tail[0] - side) / 2)
    y = round((tip[1] + tail[1] - side) / 2)
    start = (tail[0] - x, tail[1] - y)
    end = (tip[0] - x, tip[1] - y)
    grip = (start[0] + (end[0] - start[0]) * .37, start[1] + (end[1] - start[1]) * .37)
    frame = Image.new('RGBA', (side, side))
    draw = ImageDraw.Draw(frame)
    draw.line((start, end), fill=(60, 37, 22, 220), width=round(7 * thickness))
    draw.line((start, end), fill=(239, 193, 126, 255), width=round(4 * thickness))
    draw.line((start, grip), fill=(127, 79, 48, 255), width=round(5 * thickness))
    r = 2 * thickness
    draw.ellipse((end[0] - r, end[1] - r, end[0] + r, end[1] + r), fill=(126, 207, 228, 255))
    return frame, x, y
