from flask import Flask, render_template, request, jsonify
from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from faker import Faker
import hashlib
import re

app = Flask(__name__)

# Custom recognizers to boost accuracy over Presidio defaults
ssn_recognizer = PatternRecognizer(
    supported_entity="US_SSN",
    patterns=[Pattern("SSN", r"\b\d{3}-\d{2}-\d{4}\b", 0.85)],
)

credit_card_recognizer = PatternRecognizer(
    supported_entity="CREDIT_CARD",
    patterns=[
        Pattern("CC_dashes", r"\b\d{4}-\d{4}-\d{4}-\d{4}\b", 0.95),
        Pattern("CC_spaces", r"\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b", 0.95),
        Pattern("CC_continuous", r"\b\d{16}\b", 0.7),
    ],
)

phone_recognizer = PatternRecognizer(
    supported_entity="PHONE_NUMBER",
    patterns=[
        Pattern("phone_parens", r"\(\d{3}\)\s*\d{3}-\d{4}", 0.9),
        Pattern("phone_dashes", r"\b\d{3}-\d{3}-\d{4}\b", 0.85),
    ],
)

analyzer = AnalyzerEngine()
analyzer.registry.add_recognizer(ssn_recognizer)
analyzer.registry.add_recognizer(credit_card_recognizer)
analyzer.registry.add_recognizer(phone_recognizer)
fake = Faker()

# Entity type specificity — higher = wins ties over generic types
ENTITY_PRIORITY = {
    "CREDIT_CARD": 10,
    "US_SSN": 10,
    "PHONE_NUMBER": 9,
    "EMAIL_ADDRESS": 9,
    "IP_ADDRESS": 9,
    "IBAN_CODE": 9,
    "URL": 8,
    "US_DRIVER_LICENSE": 8,
    "PERSON": 5,
    "LOCATION": 5,
    "DATE_TIME": 3,
    "NRP": 2,
}

def fake_phone_matching_format(original):
    """Generate a fake phone in the same format as the original."""
    digits = re.sub(r"\D", "", fake.numerify("##########"))
    if re.match(r"\(\d{3}\)", original):
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if re.match(r"\d{3}-\d{3}-\d{4}", original):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def fake_cc_matching_format(original):
    """Generate a fake credit card preserving the delimiter style."""
    raw = fake.credit_card_number(card_type="visa16")
    if "-" in original:
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"
    if " " in original:
        return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:16]}"
    return raw[:16]


FAKER_MAP = {
    "PERSON": lambda: fake.name(),
    "EMAIL_ADDRESS": lambda: fake.email(),
    "PHONE_NUMBER": lambda: fake.phone_number(),
    "CREDIT_CARD": lambda: fake.credit_card_number(),
    "US_SSN": lambda: fake.ssn(),
    "IP_ADDRESS": lambda: fake.ipv4(),
    "DATE_TIME": lambda: fake.date(),
    "LOCATION": lambda: fake.city(),
    "URL": lambda: fake.url(),
    "US_DRIVER_LICENSE": lambda: f"DL-{fake.bothify('???########')}",
    "IBAN_CODE": lambda: fake.iban(),
    "NRP": lambda: fake.language_name(),
    "US_BANK_NUMBER": lambda: fake.numerify("#########"),
    "_EXPIRY": lambda: f"{fake.random_int(1,12):02d}/{fake.random_int(25,30):02d}",
}


def make_consistent_fake(entity_type, original_text, seen):
    """Same input always produces the same fake output within one request."""
    key = (entity_type, original_text.lower().strip())
    if key not in seen:
        seed = int(hashlib.md5(original_text.lower().strip().encode()).hexdigest()[:8], 16)
        Faker.seed(seed)

        if entity_type == "PHONE_NUMBER":
            seen[key] = fake_phone_matching_format(original_text)
        elif entity_type == "CREDIT_CARD":
            seen[key] = fake_cc_matching_format(original_text)
        elif entity_type == "PERSON":
            # If it's a single word (likely first or last name only), generate accordingly
            if " " not in original_text.strip():
                seen[key] = fake.first_name()
            else:
                seen[key] = fake.name()
        else:
            generator = FAKER_MAP.get(entity_type)
            if generator:
                seen[key] = generator()
            else:
                seen[key] = f"[{entity_type}]"

        Faker.seed(None)
    return seen[key]


def clamp_to_line(text, start, end):
    """Prevent entity spans from crossing newline boundaries."""
    newline_pos = text.find("\n", start, end)
    if newline_pos != -1:
        return start, newline_pos
    return start, end


def anonymize(text):
    results = analyzer.analyze(
        text=text,
        language="en",
        score_threshold=0.35,
    )

    # Clamp entities to line boundaries (prevents name eating next line)
    clamped = []
    for r in results:
        new_start, new_end = clamp_to_line(text, r.start, r.end)
        if new_end > new_start:
            r.start = new_start
            r.end = new_end
            clamped.append(r)

    # Build set of character positions that are JSON keys only (not values)
    # Match "key": but only protect the key part including its quotes
    json_key_positions = set()
    json_keys_by_line = {}
    for m in re.finditer(r'"([^"]+)"\s*:', text):
        key_name = m.group(1).lower()
        key_start = m.start()
        key_end = text.index('"', key_start + 1) + 1
        for i in range(key_start, key_end):
            json_key_positions.add(i)
        # Track which key name applies to each line
        line_num = text[:m.start()].count('\n')
        json_keys_by_line[line_num] = key_name

    # US state abbreviations
    US_STATES = {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }

    # JSON key names that indicate name-type values
    NAME_KEYS = {"name", "first_name", "last_name", "full_name", "contact_name"}

    # Filter out false positives
    filtered_fp = []
    for r in clamped:
        span = text[r.start:r.end].strip()
        line_num = text[:r.start].count('\n')
        line_key = json_keys_by_line.get(line_num, "")

        # Skip any entity that overlaps with a JSON key
        if json_key_positions & set(range(r.start, r.end)):
            continue

        # Skip LOCATION on 2-letter US state abbreviations
        if r.entity_type == "LOCATION" and span.strip('"') in US_STATES:
            continue

        # Skip PERSON on values that contain digits (IDs like "INC-44821", "EMP-00847")
        if r.entity_type == "PERSON" and re.search(r"\d", span):
            continue

        # Skip single-word short PERSON matches (e.g. "Covers", "Notes")
        # BUT allow them if they're inside a name-typed JSON key
        if r.entity_type == "PERSON" and " " not in span and len(span) < 4:
            if line_key not in NAME_KEYS:
                continue

        # Skip NRP on values that look like IDs/codes (contain digits or dashes with letters)
        if r.entity_type == "NRP" and re.search(r"\d", span):
            continue

        # Skip DATE_TIME on values that are clearly not dates
        if r.entity_type == "DATE_TIME":
            clean = span.strip('"').strip()
            # Pure 5-digit zip codes (e.g. "60614")
            if re.match(r"^\d{5}$", clean):
                continue
            # MM/YY short expiry — replace with fake MM/YY, not a full date
            if re.match(r"^\d{2}/\d{2}$", clean):
                r.entity_type = "_EXPIRY"
                filtered_fp.append(r)
                continue

        filtered_fp.append(r)

    # Inject synthetic PERSON entities for JSON name fields that NER missed
    for m in re.finditer(r'"(first_name|last_name|full_name|name|contact_name)"\s*:\s*"([^"]+)"', text):
        val_start = m.start(2)
        val_end = m.end(2)
        # Check if this span is already covered
        already_covered = any(
            r.start <= val_start and r.end >= val_end
            for r in filtered_fp
        )
        if not already_covered:
            from presidio_analyzer import RecognizerResult
            synth = RecognizerResult(
                entity_type="PERSON",
                start=val_start,
                end=val_end,
                score=0.9,
            )
            filtered_fp.append(synth)

    # Resolve overlaps: prefer higher score, then entity priority, then longer span
    filtered_fp.sort(
        key=lambda r: (
            -r.score,
            -ENTITY_PRIORITY.get(r.entity_type, 0),
            -(r.end - r.start),
        )
    )
    final = []
    taken = set()
    for r in filtered_fp:
        span = set(range(r.start, r.end))
        if not span & taken:
            final.append(r)
            taken |= span

    # Expand PERSON entities to full JSON string values when partially matched
    # e.g. if only "Raghavan" is detected inside "Priya Raghavan", expand to full value
    for r in final:
        if r.entity_type == "PERSON":
            # Check if this entity sits inside a JSON string value: "..."
            # Walk left to find opening quote
            left = r.start - 1
            while left >= 0 and text[left] not in '"\n':
                left -= 1
            right = r.end
            while right < len(text) and text[right] not in '"\n':
                right += 1
            if left >= 0 and text[left] == '"' and right < len(text) and text[right] == '"':
                expanded = text[left + 1:right].strip()
                # Only expand if the surrounding text looks name-like (letters/spaces/periods)
                if expanded and re.match(r'^[A-Za-z\s.\'-]+$', expanded) and len(expanded) <= 60:
                    # Make sure we're not expanding into a JSON key
                    if left not in json_key_positions:
                        r.start = left + 1
                        r.end = right

    # Sort by start descending so replacements don't shift indices
    final.sort(key=lambda r: r.start, reverse=True)

    seen = {}
    entity_counts = {}
    anonymized = text
    for result in final:
        original = text[result.start:result.end]
        replacement = make_consistent_fake(result.entity_type, original, seen)
        anonymized = anonymized[:result.start] + replacement + anonymized[result.end:]
        etype = result.entity_type.lstrip("_")
        entity_counts[etype] = entity_counts.get(etype, 0) + 1

    return anonymized, entity_counts


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/anonymize", methods=["POST"])
def anonymize_route():
    data = request.get_json()
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"result": "", "entities": {}})
    result, entity_counts = anonymize(text)
    return jsonify({"result": result, "entities": entity_counts})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
