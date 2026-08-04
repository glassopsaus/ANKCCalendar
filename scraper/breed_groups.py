#!/usr/bin/env python3
"""
Breed-group inference for CONFORMATION shows (narrowed, honest heuristic).

WHY THIS EXISTS: some users want to filter conformation shows by ANKC breed
group (Group 1 Toys ... Group 7 Non-Sporting). Our event data has no structured
breed field, but a large share of conformation shows are run by single-breed
clubs whose name states the breed (e.g. "Labrador Retriever Club of NSW"). For
CONFORMATION shows specifically, a single-breed club's show is reliably that
breed's group — so we can infer a breed group from the club name.

IMPORTANT — SCOPE AND LIMITS (kept deliberately narrow so we never mislead):
  * Only CONFORMATION events are tagged. Trials (obedience, agility, scent work,
    etc.) run by a breed club are almost always OPEN to all breeds, so inferring
    a breed restriction from the club name would be WRONG for them. The caller
    must only apply this to category == "Conformation".
  * Only SINGLE-BREED clubs map. All-breeds/kennel clubs, region committees and
    multi-breed group clubs are left UNTAGGED (breed_group = None) rather than
    guessed. Group/"society" clubs that cover a whole ANKC group (e.g. "Hound
    Club", "Gundog Society") map to that group but NOT a specific breed.
  * This is an INFERENCE, not entry eligibility. It reflects the typical case;
    the schedule/club is authoritative. The UI should label it as inferred.

ANKC groups (confirmed against ankc.org.au):
  1 Toys | 2 Terriers | 3 Gundogs | 4 Hounds | 5 Working Dogs |
  6 Utility | 7 Non-Sporting

OUTPUT: infer_breed_group(club_or_title) -> (group_label, breed_label) or
(None, None). group_label is e.g. "Group 3 (Gundogs)".
"""

import re

GROUP_LABELS = {
    1: "Group 1 (Toys)",
    2: "Group 2 (Terriers)",
    3: "Group 3 (Gundogs)",
    4: "Group 4 (Hounds)",
    5: "Group 5 (Working Dogs)",
    6: "Group 6 (Utility)",
    7: "Group 7 (Non-Sporting)",
}

# Breed / breed-keyword -> ANKC group number. Ordered longest/most-specific
# first at match time. Only breeds actually seen in the data (plus common
# neighbours) are included; anything unlisted stays UNTAGGED. Group assignments
# follow ANKC (note: several guardian/utility breeds ANKC places in Group 6
# Utility, and herding breeds in Group 5 Working — these differ from AKC).
_BREED_GROUP = {
    # --- Group 3 Gundogs ---
    "golden retriever": 3, "labrador retriever": 3, "labrador": 3,
    "retriever": 3, "cocker spaniel": 3, "springer spaniel": 3,
    "field spaniel": 3, "sporting spaniel": 3, "spaniel": 3,
    "german shorthaired pointer": 3, "pointer": 3, "weimaraner": 3,
    "vizsla": 3, "setter": 3, "brittany": 3, "gundog": 3, "gun dog": 3,
    # --- Group 2 Terriers ---
    "staffordshire bull terrier": 2, "american staffordshire terrier": 2,
    "bull terrier": 2, "boston terrier": 2, "airedale": 2,
    "scottish terrier": 2, "wheaten": 2, "fox terrier": 2, "cairn": 2,
    "west highland": 2, "terrier": 2,
    # --- Group 1 Toys ---
    "chihuahua": 1, "papillon": 1, "pomeranian": 1, "japanese chin": 1,
    "maltese": 1, "havanese": 1, "pug": 1, "toy": 1,
    # --- Group 4 Hounds ---
    "dachshund": 4, "beagle": 4, "basset": 4, "basenji": 4, "saluki": 4,
    "borzoi": 4, "afghan": 4, "greyhound": 4, "whippet": 4, "deerhound": 4,
    "wolfhound": 4, "ridgeback": 4, "harrier": 4, "foxhound": 4,
    "elkhound": 4, "hound": 4,
    # --- Group 5 Working Dogs (ANKC herding) ---
    "german shepherd": 5, "border collie": 5, "bearded collie": 5,
    "collie": 5, "kelpie": 5, "cattle dog": 5, "shetland sheepdog": 5,
    "old english sheepdog": 5, "sheepdog": 5, "welsh corgi": 5, "corgi": 5,
    "malinois": 5, "briard": 5, "vallhund": 5, "lapphund": 5,
    # --- Group 6 Utility (ANKC guardians/working-guard/spitz) ---
    "dobermann": 6, "doberman": 6, "boxer": 6, "bullmastiff": 6,
    "mastiff": 6, "siberian husky": 6, "husky": 6, "malamute": 6,
    "samoyed": 6, "rottweiler": 6, "bernese": 6, "great dane": 6,
    "newfoundland": 6, "akita": 6, "leonberger": 6, "pinscher": 6,
    "saint bernard": 6, "st bernard": 6,
    # --- Group 7 Non-Sporting ---
    "british bulldog": 7, "french bulldog": 7, "bulldog": 7, "dalmatian": 7,
    "poodle": 7, "keeshond": 7, "shar pei": 7, "chow": 7, "lhasa": 7,
    "shih tzu": 7, "schipperke": 7, "tibetan spaniel": 7, "tibetan": 7,
    "bichon": 7, "spitz": 7,
    # NOTE: "schnauzer" is intentionally OMITTED — Miniature Schnauzer is
    # Group 6 (Utility) but Giant/Standard placement and club naming are
    # ambiguous from a club name alone, so we don't guess.
}

# Clubs that are NOT single-breed and must never be tagged (all-breeds, regional
# committees, kennel clubs, sporting-committee bodies). If any of these appear,
# skip inference — even if a breed word also appears incidentally.
_NOT_SINGLE_BREED = re.compile(
    r"all\s*breeds?|kennel\s+club|kennel\s+association|canine\s+(club|association|"
    r"committee|council)|\bregion\b|\bcommittee\b|working\s+party|agricultural|"
    r"\ba\s*[&h]\s*(society|association)|show\s+society|\bp\s*[&a]\b|"
    r"district\s+kennel|obedience|training\s+club|dog\s+sports|dog\s+club\b",
    re.I)

# Longest keys first so "staffordshire bull terrier" beats "terrier", etc.
_KEYS_BY_LEN = sorted(_BREED_GROUP.keys(), key=len, reverse=True)

# "Open" as an ENTRY-ELIGIBILITY marker: a breed club can host an event that is
# open to ALL breeds, signalled by "Open" directly before a discipline/trial
# word (e.g. "Open Scent Work Trial", "Open Agility Trial", a leading
# "Open Trial"). When present, the event is NOT breed-restricted even though a
# breed club runs it, so we must NOT tag it. This is DELIBERATELY narrow so it
# does not fire on:
#   * "Championship & Open Show" / "Open Show"  -> conformation SHOW CLASS
#   * "Open Stakes" / "All Age" / "Novice"      -> retrieving/obedience LEVEL
# both of which remain breed-restricted.
_OPEN_ALL_BREEDS = re.compile(
    r"\bopen\b(?!\s+(?:show|stake|stakes))\s+"
    r"(?:scent\s*work|agility|jumping|tracking|track|obedience|rally|trick|"
    r"dances|herding|endurance|lure|sprint|sled|trial|trials|test|tests)",
    re.I)
_OPEN_LEADING = re.compile(r"^\s*open\s+(?:trial|test)\b", re.I)


def is_open_all_breeds(title):
    """True if a title marks the event as open to ALL breeds via a bare "Open
    <discipline>" (not "Open Show" class, not "Open Stakes" level)."""
    if not title:
        return False
    return bool(_OPEN_ALL_BREEDS.search(title) or _OPEN_LEADING.search(title))


# Disciplines that are INHERENTLY restricted to a breed group by their nature,
# regardless of the club. Retrieving trials, Retrieving Ability Tests for
# Gundogs (RATG/RTG) and Field Trials are gundog-only events -> Group 3.
_DISCIPLINE_GROUP = {
    "Retrieving": 3,
    "Field Trial": 3,
    "RATG": 3,          # legacy raw code; normally normalised to Retrieving
}


def group_for_discipline(category):
    """Return the ANKC group LABEL a discipline is inherently restricted to, or
    None. E.g. Retrieving / Field Trial -> Group 3 (Gundogs). This is more
    reliable than club-name inference because the discipline itself dictates the
    eligible group, and it applies to TRIALS (not just conformation)."""
    g = _DISCIPLINE_GROUP.get(category)
    return GROUP_LABELS[g] if g else None


def infer_breed_group(text, title=None):
    """Infer (group_label, breed_keyword) from an event's club/title for a
    SINGLE-BREED club (applies to shows AND trials — a breed club's trial is
    restricted to that breed's group). Returns (None, None) when:
      * the club isn't a recognisable single breed, or is a known
        all-breeds/multi-breed/regional body (via _NOT_SINGLE_BREED), or
      * the event title marks it OPEN to all breeds (bare "Open <discipline>"),
        even though a breed club runs it.
    `title` defaults to `text`; pass the event title explicitly when the club
    name and title differ so the open-to-all-breeds check sees the full title.
    """
    if not text:
        return None, None
    # A breed club's event can be explicitly open to all breeds -> not tagged.
    if is_open_all_breeds(title if title is not None else text):
        return None, None
    low = text.lower()
    # Never infer for all-breeds / multi-breed / regional bodies.
    if _NOT_SINGLE_BREED.search(low):
        return None, None
    for key in _KEYS_BY_LEN:
        idx = low.find(key)
        if idx == -1:
            continue
        before = low[idx - 1] if idx > 0 else " "
        after_i = idx + len(key)
        after = low[after_i] if after_i < len(low) else " "
        if not before.isalnum() and not after.isalnum():
            return GROUP_LABELS[_BREED_GROUP[key]], key
    return None, None


if __name__ == "__main__":
    tests = [
        "Labrador Retriever Club of NSW",
        "Dobermann Club of NSW Inc",
        "Afghan Hound Club of NSW",
        "Scottish Terrier Club Inc",
        "Papillon Club of Vic Inc",
        "Dalmatian Club of NSW Inc",
        "Border Collie Club of NSW Inc",
        "Hound Club of Vic Inc",
        "Wollongong & District Kennel Club Inc",   # all-breeds -> None
        "DOGS NSW Illawarra & South Eastern Region",  # region -> None
        "Dobermann Club of NSW Inc",  # trials would be excluded by caller
    ]
    for t in tests:
        g, b = infer_breed_group(t)
        print(f"  {g or '(none)':22} breed={b or '-':16} <- {t}")
