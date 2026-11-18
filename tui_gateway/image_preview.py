"""Bounded terminal previews shared by the standalone and canonical gateways."""
from agent.terminal_image import local_image_path, render_inline_image, resolve_inline_mode

# Mirrors ui-tui/src/config/limits.ts: no client gets a bigger preview.
_MAX_PREVIEW_COLS = 64
_MAX_PREVIEW_ROWS = 16


def render_preview(params: dict, display: dict) -> dict:
    # The client's terminal, never the daemon's: one owner serves many clients.
    mode = resolve_inline_mode(display.get('inline_images', 'auto'), graphics=params.get('graphics') is True,
                               detected='kitty' if params.get('terminal') == 'kitty' else 'unicode')
    path = local_image_path(params.get('path'))
    cols = min(int(params.get('cols') or 0), _MAX_PREVIEW_COLS)
    rows = min(int(params.get('rows') or 0), _MAX_PREVIEW_ROWS)
    image = render_inline_image(path, mode=mode, max_cols=cols, max_rows=rows) if path else None
    if image is None:
        return {'mode': 'off'}
    if image.mode == 'kitty':
        return {'mode': 'kitty', 'cols': image.cols, 'rows': image.rows, 'color': image.color,
                'transmit': image.transmit, 'placeholder': image.placeholder}
    return {'mode': 'unicode', 'cols': image.cols, 'rows': image.rows,
            'cells': [[[*top, *bottom] for top, bottom in row] for row in image.cells]}
