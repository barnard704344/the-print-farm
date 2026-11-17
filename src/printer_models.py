"""Resolve display-only printer pictures from explicit models or printer names."""
import re

# Only bundled files can be returned; configuration is never used as an image URL.
MODELS = {
    'p1s': ('Bambu Lab P1S', 'bambu-p1s.png'),
    'p1p': ('Bambu Lab P1P', 'bambu-p1p.png'),
    'x1c': ('Bambu Lab X1 Carbon', 'bambu-x1c.png'),
    'x1e': ('Bambu Lab X1E', 'bambu-x1e.png'),
    'x1': ('Bambu Lab X1', 'bambu-x1.png'),
    'a1-mini': ('Bambu Lab A1 mini', 'bambu-a1-mini.png'),
    'a1': ('Bambu Lab A1', 'bambu-a1.png'),
    'voron-2.4': ('Voron 2.4', 'voron-2.4.png'),
    'voron-trident': ('Voron Trident', 'voron-trident.png'),
    'voron-switchwire': ('Voron Switchwire', 'voron-switchwire.png'),
    'voron-0': ('Voron Zero', 'voron-0.png'),
    'voron': ('Voron', 'voron-2.4.png'),
}


def _match(value, printer_type):
    value = re.sub(r'[^a-z0-9.]+', ' ', str(value or '').lower()).strip()
    if printer_type == 'klipper':
        if re.search(r'(?:\bvoron\s+(?:v?0(?:\.[12])?|zero)\b|\bv0(?:\.[12])?\b|^(?:0(?:\.[12])?|zero)$)', value):
            return 'voron-0'
        for word, model in [('trident', 'voron-trident'), ('switchwire', 'voron-switchwire')]:
            if re.search(r'\b' + word + r'\b', value):
                return model
        if re.search(r'\b(?:voron\s*)?(?:v?2\.4)\b', value):
            return 'voron-2.4'
        if re.search(r'\bvoron\b', value):
            return 'voron'
    else:
        for pattern, model in [
            (r'\ba1\s*mini\b', 'a1-mini'),
            (r'\bx1\s*(?:carbon|c)\b', 'x1c'),
            (r'\bx1\s*e\b', 'x1e'),
            (r'\bp1s\b', 'p1s'), (r'\bp1p\b', 'p1p'),
            (r'\bx1\b', 'x1'), (r'\ba1\b', 'a1'),
        ]:
            if re.search(pattern, value):
                return model
    return ''


def printer_model_details(name, config):
    """An explicit model overrides name guessing, including unknown models."""
    printer_type = str(config.get('type', 'bambulab')).lower()
    explicit = str(config.get('model') or '').strip()
    model = _match(explicit or name, printer_type)
    label, image = MODELS.get(model, (explicit or ('Klipper printer' if printer_type == 'klipper' else 'Bambu Lab printer'), ''))
    return {
        'printer_model': model,
        'printer_model_name': label,
        'printer_image': 'printers/' + image if image else '',
    }
