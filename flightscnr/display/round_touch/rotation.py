"""Display rotation — logical draw buffer vs physical screen and touch."""

import time

import pygame

from display.round_touch import frame_debug, theme


_rot_base: pygame.Surface | None = None
_rot_base_key = None
_prev_sweep_rect: pygame.Rect | None = None
# Radar layer generation seen but not yet rotated/swapped (one-frame pipeline).
_pending_key = None
# Pre-rotated next base prepared between frames (see prewarm_base).
_next_base: pygame.Surface | None = None
_next_base_key = None
# A full-frame present() ran (modal, other screen); the next fast radar frame
# must repaint the whole display rather than trust dirty-rect state.
_needs_full = False


def prewarm_base(base_layer: pygame.Surface, layer_gen: int) -> None:
    """Rotate the rebuilt radar layer in idle time between frames so the next
    presented frame only pays the swap blit + flip, never the rotate.

    ``base_layer`` must be a private surface (caller snapshot); never pass the
    live published radar layer — concurrent blit/present will lock-conflict.
    """
    global _next_base, _next_base_key
    rotation = rotation_degrees()
    # Key on generation only — the snapshot surface id differs from the
    # published layer id that present_radar_sweep sees.
    key = (layer_gen, rotation, theme.SIZE)
    if key == _rot_base_key or key == _next_base_key:
        return
    _t = time.perf_counter()
    if rotation == 0:
        # Already a private snapshot from the radar rebuild path.
        _next_base = base_layer
    else:
        _next_base = pygame.transform.rotate(base_layer, -rotation)
    _next_base_key = key
    if frame_debug.ENABLED:
        frame_debug.stage("0_prewarm_rotate", time.perf_counter() - _t)


def normalize_degrees(degrees: int) -> int:
    degrees = int(degrees) % 360
    if degrees not in (0, 90, 180, 270):
        degrees = round(degrees / 90) * 90 % 360
    return degrees


def rotation_degrees() -> int:
    """Clockwise UI rotation (persisted settings, else DISPLAY_ROTATION env)."""
    try:
        from display.round_touch import settings

        return normalize_degrees(settings.display_rotation())
    except Exception:
        pass
    try:
        from config import DISPLAY_ROTATION
    except ImportError:
        import os

        try:
            DISPLAY_ROTATION = int(os.environ.get("DISPLAY_ROTATION", "0"))
        except (TypeError, ValueError):
            DISPLAY_ROTATION = 0
    return normalize_degrees(DISPLAY_ROTATION)


def _physical_display_size() -> tuple[int, int]:
    surface = pygame.display.get_surface()
    if surface is None:
        return theme.SIZE, theme.SIZE
    return surface.get_size()


def viewport_offset(
    display_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Top-left offset of the centered square UI in the physical display."""
    width, height = display_size or _physical_display_size()
    return (
        (int(width) - theme.SIZE) // 2,
        (int(height) - theme.SIZE) // 2,
    )


def in_viewport(
    x: float,
    y: float,
    display_size: tuple[int, int] | None = None,
) -> bool:
    """Return True when a physical coordinate is inside the square UI."""
    offset_x, offset_y = viewport_offset(display_size)
    return (
        offset_x <= x < offset_x + theme.SIZE
        and offset_y <= y < offset_y + theme.SIZE
    )


def to_logical(
    x: float,
    y: float,
    display_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Map a physical display/touch coordinate into the square draw buffer."""
    offset_x, offset_y = viewport_offset(display_size)
    x -= offset_x
    y -= offset_y

    side = theme.SIZE
    rotation = rotation_degrees()
    if rotation == 0:
        return int(x), int(y)
    if rotation == 90:
        return int(y), int(side - 1 - x)
    if rotation == 180:
        return int(side - 1 - x), int(side - 1 - y)
    return int(side - 1 - y), int(x)


def present(display: pygame.Surface, frame: pygame.Surface) -> None:
    """Blit the logical frame onto the physical display, applying rotation."""
    global _prev_sweep_rect, _pending_key, _needs_full
    # Full-frame present invalidates the dirty-sweep erase rect.
    _prev_sweep_rect = None
    _pending_key = None
    _needs_full = True
    rotation = rotation_degrees()
    if rotation == 0:
        if display.get_size() == frame.get_size():
            display.blit(frame, (0, 0))
        else:
            display.fill((0, 0, 0))
            display.blit(frame, _center_offset(display, frame))
        return

    rotated = pygame.transform.rotate(frame, -rotation)
    if display.get_size() == rotated.get_size():
        display.blit(rotated, (0, 0))
        return
    display.fill((0, 0, 0))
    display.blit(rotated, _center_offset(display, rotated))


def present_radar_sweep(
    display: pygame.Surface,
    base_layer: pygame.Surface,
    layer_gen: int,
    sweep_angle_logical: float,
    sweep_color,
) -> None:
    """Blit a cached rotated radar base, then draw the sweep in display space.

    Avoids re-rotating the full 720×720 frame every sweep tick (was ~4.5ms on
    Pi with DISPLAY_ROTATION=90). The static layer is rotated only when it
    rebuilds (~10Hz); each frame restores the previous sweep AABB from the
    cached base and paints a new wedge. Uses display.update(dirty) so X11
    doesn't re-push the whole framebuffer.
    """
    global _rot_base, _rot_base_key, _prev_sweep_rect, _pending_key, _needs_full
    global _next_base, _next_base_key
    from display.round_touch import draw

    rotation = rotation_degrees()
    # Match prewarm_base — generation identifies the layer contents.
    key = (layer_gen, rotation, theme.SIZE)
    origin_off = (0, 0)
    full_refresh = False

    stale = _rot_base is None or _rot_base_key != key
    swap_in = False
    if stale and _next_base is not None and _next_base_key == key:
        # Rotation was prewarmed between frames; just swap it in.
        _rot_base = _next_base
        _rot_base_key = key
        _next_base = None
        _next_base_key = None
        _pending_key = None
        stale = False
        swap_in = True
    elif stale and _rot_base is not None and not _needs_full and _pending_key is None:
        # The static layer just rebuilt (~10Hz). This frame already paid the
        # rebuild cost inside draw_radar, so defer the rotate + full flip to
        # the *next* frame — otherwise both land in one frame and the beam
        # visibly steps ten times a second.
        _pending_key = key
        stale = False

    if stale:
        _t = time.perf_counter()
        try:
            if rotation == 0:
                # Copy so the async layer rebuild can't scribble on the surface
                # we erase sweep rects from (see prewarm_frame_layer).
                _rot_base = base_layer.copy()
            else:
                _rot_base = pygame.transform.rotate(base_layer, -rotation)
        except pygame.error as exc:
            # Published layer briefly locked by a concurrent rebuild/snapshot.
            if "locked" in str(exc).lower():
                if _rot_base is None:
                    return
                stale = False
            else:
                raise
        if stale:
            _rot_base_key = key
            _pending_key = None
            swap_in = True
            if frame_debug.ENABLED:
                frame_debug.stage("4r_rotate", time.perf_counter() - _t)

    if swap_in:
        if display.get_size() != _rot_base.get_size():
            display.fill((0, 0, 0))
            origin_off = _center_offset(display, _rot_base)
            display.blit(_rot_base, origin_off)
        else:
            display.blit(_rot_base, (0, 0))
        _prev_sweep_rect = None
        full_refresh = True
        _needs_full = False
    else:
        if display.get_size() != _rot_base.get_size():
            origin_off = _center_offset(display, _rot_base)
        # Erase the previous wedge by restoring that rect from the static base.
        if _prev_sweep_rect is not None:
            r = _prev_sweep_rect
            src = pygame.Rect(
                r.x - origin_off[0],
                r.y - origin_off[1],
                r.w,
                r.h,
            )
            display.blit(_rot_base, r.topleft, src)

    old_rect = _prev_sweep_rect
    # present() rotates the frame by -rotation; a logical tip at angle θ lands
    # on the display at θ - rotation (0=up on both surfaces).
    angle_disp = (sweep_angle_logical - rotation) % 360.0
    cx = origin_off[0] + _rot_base.get_width() / 2.0
    cy = origin_off[1] + _rot_base.get_height() / 2.0
    new_rect = draw.draw_sweep_line(
        display,
        angle_disp,
        sweep_color,
        width=max(2, theme.s(2)),
        origin=(cx, cy),
        radius=float(theme.SWEEP_RADIUS),
    )
    _prev_sweep_rect = new_rect

    _t = time.perf_counter()
    if full_refresh:
        pygame.display.flip()
    else:
        dirty = [r for r in (old_rect, new_rect) if r is not None]
        if dirty:
            pygame.display.update(dirty)
        else:
            pygame.display.flip()
    if frame_debug.ENABLED:
        frame_debug.stage("4r_flip" if full_refresh else "4s_update", time.perf_counter() - _t)


def _center_offset(dst: pygame.Surface, src: pygame.Surface) -> tuple[int, int]:
    return (
        (dst.get_width() - src.get_width()) // 2,
        (dst.get_height() - src.get_height()) // 2,
    )
