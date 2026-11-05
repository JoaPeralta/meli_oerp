"""Target only historical connector-owned API URLs; never portal capabilities."""
import html
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_API_URL = re.compile(r'https://api\.mercadolibre\.com/(?:shipment_labels|items/[A-Za-z0-9_-]+)(?:\?[^\s<>"\']*)?')


def clean_api_links(value):
    if not isinstance(value, str):
        return value
    def clean(match):
        original = match.group(0)
        url = urlsplit(html.unescape(original))
        pairs = parse_qsl(url.query, keep_blank_values=True)
        retained = [(key, item) for key, item in pairs if key.lower() != 'access_token']
        if retained == pairs:
            return original
        result = urlunsplit((url.scheme, url.netloc, url.path, urlencode(retained), url.fragment))
        return result.replace('&', '&amp;') if '&amp;' in original else result
    return _API_URL.sub(clean, value)


def clean_print_links(value):
    """Legacy grouping used the credential itself as a JSON dictionary key."""
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return clean_api_links(value)
    if isinstance(data, dict):
        groups = {}
        changed = False
        for index, (key, entry) in enumerate(data.items()):
            if isinstance(entry, dict) and _API_URL.match(str(entry.get('full_link') or entry.get('url') or '')):
                # Preserve group data, but never the old credential key.
                if re.fullmatch(r'legacy_group_[0-9]+_*', key):
                    groups[key] = entry
                    continue
                replacement = 'legacy_group_' + str(index)
                while replacement in data or replacement in groups:
                    replacement += '_'
                groups[replacement] = entry
                changed = True
            else:
                groups[key] = entry  # Unrecognized data is not cleanup authority.
        return clean_api_links(json.dumps(groups)) if changed else clean_api_links(value)
    return clean_api_links(value)
