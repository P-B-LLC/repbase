#!/usr/bin/env python3
"""Build core/data/exercise_library.json, the Rytivo exercise catalogue.

Pipeline (deterministic, stdlib only, Python 3.12+):
  1. read the free-exercise-db base (dist/exercises.json, Unlicense)
  2. apply base_curation.json: drop, merge, equipment fixes, explicit renames
  3. rename the rest with the naming rules below and map fields to the vocabularies
  4. derive movement pattern, disciplines and family with keyword rules
  5. apply overrides.json (keyed by final slug, any field)
  6. add the curated supplement.json entries
  7. build and de-duplicate aliases, validate, sort and write atomically

Usage:
    python3 -I Scripts/exercise_library/build_exercise_library.py \
        --base /path/to/free-exercise-db/dist/exercises.json \
        [--out core/data/exercise_library.json] [--review-tsv review.tsv]

The base file is treated as untrusted data: it is only parsed as JSON and every
field is type-checked before use.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_OUT = REPO_ROOT / "core" / "data" / "exercise_library.json"
LIBRARY_VERSION = "2026.10.29"
SOURCES = [
    {"name": "free-exercise-db", "url": "https://github.com/yuhonas/free-exercise-db", "license": "Unlicense"},
    {"name": "Rytivo curated supplement", "license": "Proprietary (Rytivo)"},
]


def _load_validator():
    sys.dont_write_bytecode = True  # keep this directory free of __pycache__
    spec = importlib.util.spec_from_file_location("validate_exercise_library", HERE / "validate_exercise_library.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = _load_validator()
slugify = V.slugify
normalize_name = V.normalize_name


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------
# Base field mapping
# ---------------------------------------------------------------------------

BASE_EQUIPMENT = {
    "barbell": "barbell",
    "dumbbell": "dumbbell",
    "kettlebells": "kettlebell",
    "cable": "cable",
    "machine": "machine",
    "body only": "bodyweight",
    "bands": "resistance_band",
    "medicine ball": "medicine_ball",
    "exercise ball": "stability_ball",
    "foam roll": "foam_roller",
    "e-z curl bar": "ez_bar",
    "other": "other",
    None: "bodyweight",
}
BASE_CATEGORY = {
    "strength": ("strength", None),
    "powerlifting": ("strength", "powerlifting"),
    "olympic weightlifting": ("olympic", "olympic_weightlifting"),
    "plyometrics": ("plyometric", None),
    "strongman": ("strength", "strongman"),
    "stretching": ("stretching", None),
    "cardio": ("cardio", None),
}
BASE_LEVEL = {"beginner": "beginner", "intermediate": "intermediate", "expert": "advanced"}
BASE_MUSCLE = {
    "abdominals": "abdominals", "abductors": "abductors", "adductors": "adductors",
    "biceps": "biceps", "calves": "calves", "chest": "chest", "forearms": "forearms",
    "glutes": "glutes", "hamstrings": "hamstrings", "lats": "lats", "lower back": "lower_back",
    "middle back": "upper_back", "neck": "neck", "quadriceps": "quadriceps",
    "shoulders": "shoulders", "traps": "traps", "triceps": "triceps",
}
BASE_FORCE = {"push": "push", "pull": "pull", "static": "static", None: None}
BASE_MECHANIC = {"compound": "compound", "isolation": "isolation", None: None}

# Equipment named in parentheses at the end of a name ("Bench Press (Barbell)").
EQUIPMENT_LABELS = {
    "barbell": "Barbell", "dumbbell": "Dumbbell", "kettlebell": "Kettlebell", "cable": "Cable",
    "machine": "Machine", "smith_machine": "Smith Machine", "ez_bar": "EZ Bar",
    "trap_bar": "Trap Bar", "resistance_band": "Band", "medicine_ball": "Medicine Ball",
    "stability_ball": "Stability Ball", "suspension_trainer": "Suspension Trainer", "plate": "Plate",
}
# Equipment words removed from a base name before the suffix is added.
EQUIPMENT_WORDS = {
    "barbell": [r"barbell"],
    "dumbbell": [r"two[- ]dumbbells?", r"dumbbells?", r"db"],
    "kettlebell": [r"with two kettlebells", r"kettlebells?"],
    "cable": [r"cables?"],
    "machine": [r"machine"],
    "smith_machine": [r"smith machine", r"smith"],
    "ez_bar": [r"e-?z[- ]?(?:curl[- ])?bar", r"ez"],
    "trap_bar": [r"trap bar", r"hex bar"],
    "resistance_band": [r"with bands?", r"bands?"],
    "medicine_ball": [r"medicine ball"],
    "stability_ball": [r"exercise ball", r"stability ball", r"physioball"],
    "suspension_trainer": [r"with straps", r"suspended", r"suspension"],
}

SMALL_WORDS = {"a", "an", "and", "or", "the", "of", "to", "on", "with", "from", "for", "at", "by", "vs", "into", "off"}
KEEP_CASE = {"EZ", "SMR", "IT", "TRX", "GHD", "RDL", "JM", "UCV", "SL", "DB", "BB", "KB", "T-Bar", "V-Bar", "L-Sit", "V-Sit", "V-Up", "Y-T-W", "90/90", "3/4"}

# Applied in order, case-insensitively, to every base name.
NAME_SUBSTITUTIONS = [
    (r"\bpush[- ]?ups?\b", "Push Up"),
    (r"\bpull[- ]?ups?\b", "Pull Up"),
    (r"\bchin[- ]?ups?\b", "Chin Up"),
    (r"\bsit[- ]?ups?\b", "Sit Up"),
    (r"\bstep[- ]?ups?\b", "Step Up"),
    (r"\bget[- ]?ups?\b", "Get Up"),
    (r"\bmuscle[- ]?ups?\b", "Muscle Up"),
    (r"\bbutt[- ]ups?\b", "Butt Up"),
    (r"\bbody[- ]up\b", "Body Up"),
    (r"\botis[- ]up\b", "Otis Up"),
    (r"\bpull[- ]ins?\b", "Pull In"),
    (r"\bone[- ]arm(?:ed)?\b", "Single Arm"),
    (r"\bsingle[- ]arm\b", "Single Arm"),
    (r"\bone[- ]leg(?:ged)?\b", "Single Leg"),
    (r"\bsingle[- ]leg\b", "Single Leg"),
    (r"\balternate\b", "Alternating"),
    (r"\bbent[- ]over\b", "Bent Over"),
    (r"\bstiff[- ]leg(?:ged)?\b", "Stiff Leg"),
    (r"\bstraight[- ]arm\b", "Straight Arm"),
    (r"\bbent[- ]arm\b", "Bent Arm"),
    (r"\bbent[- ]knee\b", "Bent Knee"),
    (r"\bpalms?[- ]down\b", "Palms Down"),
    (r"\bpalms?[- ]up\b", "Palms Up"),
    (r"\bpalms?[- ]in\b", "Palms In"),
    (r"\blow[- ]pulley\b", "Low Pulley"),
    (r"\bhigh[- ]pulley\b", "High Pulley"),
    (r"\brear[- ]delt\b", "Rear Delt"),
    (r"\btwo[- ]arm\b", "Two Arm"),
    (r"\btriceps?\b", "Triceps"),
    (r"\bbiceps? curl", "Bicep Curl"),
    (r"\bleverage\b", "Lever"),
    (r"-smr\b", " SMR"),
    (r"\bfly(?:e|es|s)\b", "Fly"),
    (r"\bskullcrushers?\b", "Skull Crusher"),
    (r"\bhyperextensions\b", "Hyperextension"),
    (r"\bfull range-of-motion\b", "Full Range of Motion"),
    (r"\bclose-grip\b", "Close Grip"),
    (r"\bwide-grip\b", "Wide Grip"),
]
PLURALS = {
    "curls": "curl", "raises": "raise", "squats": "squat", "lunges": "lunge", "rows": "row",
    "crunches": "crunch", "dips": "dip", "shrugs": "shrug", "deadlifts": "deadlift",
    "presses": "press", "extensions": "extension", "kicks": "kick", "twists": "twist",
    "swings": "swing", "stones": "stone", "climbers": "climber", "pulls": "pull",
    "hops": "hop", "jumps": "jump", "circles": "circle", "rotations": "rotation",
    "kickbacks": "kickback", "pushdowns": "pushdown", "pulldowns": "pulldown",
    "thrusts": "thrust", "bridges": "bridge", "touchers": "toucher", "sprints": "sprint",
    "windmills": "windmill", "groiners": "groiner", "cocoons": "cocoon", "laterals": "lateral",
    "rollouts": "rollout", "drags": "drag", "throws": "throw", "slams": "slam",
    "pullovers": "pullover", "crossovers": "crossover", "squeezes": "squeeze", "wipers": "wiper",
    "mornings": "morning", "chins": "chin up", "flips": "flip", "carries": "carry",
    "walks": "walk", "lifts": "lift", "tucks": "tuck",
}
GRIP_PREFIX_RE = re.compile(r"^(close|wide|narrow|medium|reverse|neutral|mixed)[- ](grip|stance)\s+", re.I)


def title_case(text: str) -> str:
    words = text.split(" ")
    out = []
    keep = {k.lower(): k for k in KEEP_CASE}
    for i, word in enumerate(words):
        if not word:
            continue
        low = word.lower()
        if low in keep:
            out.append(keep[low])
        elif i > 0 and low in SMALL_WORDS:
            out.append(low)
        elif "-" in word and not word.startswith("-"):
            out.append("-".join(p[:1].upper() + p[1:].lower() if p else p for p in word.split("-")))
        elif word[:1].isdigit():
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def _strip_equipment_words(text: str, equipment: str) -> str:
    for pattern in EQUIPMENT_WORDS.get(equipment, []):
        text = re.sub(r"(?:^|\s|-)" + pattern + r"(?=$|\s|-)", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -")


def rule_rename(name: str, equipment: str) -> str:
    """Apply the naming convention to a base name.

    "Barbell Bench Press - Medium Grip" + barbell -> "Bench Press (Barbell)"
    "Close-Grip Barbell Bench Press" + barbell    -> "Bench Press - Close Grip (Barbell)"
    """
    text = re.sub(r"\s+", " ", name).strip()
    for pattern, repl in NAME_SUBSTITUTIONS:
        text = re.sub(pattern, repl, text, flags=re.I)

    qualifiers = []
    paren = re.search(r"\s*\(([^)]*)\)\s*$", text)
    if paren:
        qualifiers.append(paren.group(1))
        text = text[: paren.start()]
    if " - " in text:
        text, tail = text.split(" - ", 1)
        qualifiers.insert(0, tail)
    grip = GRIP_PREFIX_RE.match(text)
    if grip:
        qualifiers.insert(0, f"{grip.group(1)} {grip.group(2)}")
        text = text[grip.end():]

    text = _strip_equipment_words(text, equipment)
    cleaned_quals = []
    for q in qualifiers:
        q = _strip_equipment_words(q, equipment)
        if q and q.lower() not in ("medium grip", "medium"):
            cleaned_quals.append(q)

    words = []
    for word in text.split(" "):
        words.append(PLURALS.get(word.lower(), word))
    text = title_case(" ".join(words))
    quals = [title_case(" ".join(PLURALS.get(w.lower(), w) for w in q.split(" "))) for q in cleaned_quals]

    out = text
    if quals:
        out += " - " + ", ".join(quals)
    label = EQUIPMENT_LABELS.get(equipment)
    if label and equipment != "plate":
        out += f" ({label})"
    return re.sub(r"\s+", " ", out).strip()


def split_name(name: str) -> tuple[str, str, str]:
    """"Bench Press - Close Grip (Barbell)" -> ("Bench Press", "Close Grip", "Barbell")."""
    label = ""
    m = re.search(r"\s*\(([^)]*)\)$", name)
    if m:
        label = m.group(1)
        name = name[: m.start()]
    qual = ""
    if " - " in name:
        name, qual = name.split(" - ", 1)
    return name.strip(), qual.strip(), label.strip()


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

TYPO_FIXES = [
    (r"\bacclerate\b", "accelerate"), (r"\basborbing\b", "absorbing"), (r"\bexerise\b", "exercise"),
    (r"\bfoward\b", "forward"), (r"\bguage\b", "gauge"), (r"\binitate\b", "initiate"),
    (r"\bkettlebll\b", "kettlebell"), (r"\bketttlebells\b", "kettlebells"), (r"\bmedine\b", "medicine"),
    (r"\bpilling\b", "pulling"), (r"\bpostion\b", "position"), (r"\bthrought\b", "through"),
    (r"\bthrougout\b", "throughout"), (r"\bwont\b", "won't"), (r"\byourshoulder\b", "your shoulder"),
    (r"\bzurcher\b", "Zercher"), (r"\bdumbell\b", "dumbbell"), (r"\bparner\b", "partner"),
    (r"\bYou core\b", "Your core"), (r"\bth starting\b", "the starting"),
    (r"\byour breathe in\b", "you breathe in"), (r"\bYou should pressed firmly against he back\b", "You should be pressed firmly against the back"),
    (r"\bdoing actually doing\b", "actually doing"), (r"\bTip: My preference is with the hands next to me\.\s*", ""),
    (r"\s*\(my preference\)", ""), (r"\bchinup\b", "chin-up"), (r"\bpullup\b", "pull-up"),
    (r"\bbesides your head\b", "beside your head"), (r"\bathletes effort\b", "athlete's effort"),
    (r"\bBegin my leaning\b", "Begin by leaning"), (r"\s*\(my preferred way\)", ""),
    (r"To ensure the best range of motion, I recommend using small plates \(25-lb ones\) as opposed to larger plates \(like 35-45lb ones\)",
     "For the best range of motion, use small plates (25 lb) rather than larger ones (35-45 lb)"),
    (r"(?<=[a-z])\.(?=[A-Z][a-z])", ". "), (r"\bTips?:\s+", ""), (r"\bTip\s+(?=[A-Z])", ""),
]
LONG_STEP = 400
CHUNK = 300


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


def clean_instructions(steps) -> list[str]:
    out = []
    for raw in steps:
        if not isinstance(raw, str):
            continue
        for piece in raw.split("\n"):
            s = re.sub(r"\s+", " ", piece).strip()
            s = re.sub(r"\s*li>\s*$", "", s)
            s = re.sub(r"(?<=\.)v$", "", s)
            s = re.sub(r"(?<=[.!?])\s+\d+$", "", s)
            s = re.sub(r"\s+Tips?:\s*$", "", s)
            s = re.sub(r"\s+([,.;:])", r"\1", s)
            s = re.sub(r"\.\s*\.", ".", s)
            for pattern, repl in TYPO_FIXES:
                s = re.sub(pattern, repl, s)
            s = s.strip()
            if not s:
                continue
            s = s[0].upper() + s[1:]
            if s[-1] not in ".!?)\"":
                s += "."
            out.append(s)
    # A single paragraph that holds a whole exercise is split into sentences.
    if len(out) == 1 and len(_split_sentences(out[0])) > 1:
        out = _split_sentences(out[0])
    # Very long steps are regrouped into shorter steps at sentence boundaries.
    result = []
    for step in out:
        if len(step) <= LONG_STEP:
            result.append(step)
            continue
        chunk = ""
        for sentence in _split_sentences(step):
            if chunk and len(chunk) + 1 + len(sentence) > CHUNK:
                result.append(chunk)
                chunk = sentence
            else:
                chunk = f"{chunk} {sentence}".strip()
        if chunk:
            result.append(chunk)
    return result


# ---------------------------------------------------------------------------
# Derivation rules (movement pattern, disciplines, family, category)
# ---------------------------------------------------------------------------

def _rx(pattern):
    return re.compile(pattern, re.I)


# (regex, pattern); matched against the name without its " - qualifier"; first match wins.
PATTERN_RULES = [
    (_rx(r"foam roll|\bsmr\b|massage ball|lacrosse ball|roller massage"), "mobility"),
    (_rx(r"calf raise|calf press|(?<!clean )(?<!snatch )\bshrug\b|kickback|hip extension|hip abduction|hip adduction|hip flexion|tibialis raise|toe raise|monster walk|band walk|lateral walk|clamshell"), "isolation"),
    (_rx(r"\bcarry\b|farmer|yoke walk|suitcase carry|waiter.?s walk|overhead walk|conan|power stairs"), "carry"),
    (_rx(r"reverse fly|rear delt fly|rear delt raise|rear lateral raise|pull apart|face pull"), "horizontal_pull"),
    (_rx(r"\bfly\b|crossover|pullover|around the world|svend press|chest squeeze|iron cross|straight arm pulldown|tate press|incline shoulder raise|lateral raise|front raise|external rotation|internal rotation|scaption|y-t-w"), "isolation"),
    (_rx(r"split squat|lunge|step up|skater squat|cossack|curtsy|split stance"), "lunge"),
    (_rx(r"\bsled\b.*\b(push|drag|pull|sprint)|prowler|bear crawl|crab walk|\bcrawl\b|\bsprint|\brun\b|running|jogging|walking|\bwalk\b|treadmill|skipping|carioca|shuffle|\bskating\b|hiking|stair climber|step mill|elliptical|cycling|stationary bike|recumbent bike|air bike|rowing machine|ski erg|jump rope|double under|single under|handstand walk"), "locomotion"),
    (_rx(r"wood ?chop|russian twist|landmine 180|landmine rotation|\btwist\b|judo flip|rotational|windshield wiper|torso rotation|pirate ship"), "rotation"),
    (_rx(r"pulldown|pull down|pull up|chin up|muscle up|rope climb|front lever|back lever|skin the cat|human flag|chin crunch|typewriter|upright row|dead hang|london bridge"), "vertical_pull"),
    (_rx(r"handstand push up|overhead press|shoulder press|military press|push press|arnold press|bradford press|landmine press|\bz press|pike push up|log press|see ?saw press|bent press|para press|side press|jammer|behind the neck press|circus bell|seated press|anti-gravity press|cuban press|scott press|viking press|axle press|sots press|wall walk"), "vertical_push"),
    (_rx(r"\bclean\b(?! deadlift)|\bsnatch\b(?! deadlift)|\bjerk\b|\bhigh pull\b|thruster|\bcluster\b|wall ball|man maker|devil press|\bburpee|\bslam\b|\bthrow\b|chest pass|\bpass\b|\btoss\b|chest push|tire flip|atlas stone|keg load|sandbag load|stone|sledgehammer|heavy bag|plyo|clap|rack delivery|battle rope"), "explosive"),
    (_rx(r"\bjump|\bhop\b|\bbound\b|\bleap\b|box skip|\bskip\b|tuck jump|star jump|bench sprint|push off"), "explosive"),
    (_rx(r"kettlebell swing|\bswing\b|deadlift|good morning|hip thrust|glute bridge|hip bridge|\bbridge\b|hip lift|pull through|hyperextension|back extension|glute ham|nordic|rack pull|block pull|romanian|\brdl\b|reverse hyper|butt lift"), "hinge"),
    (_rx(r"squat|leg press|wall sit|sit to stand|hack|sissy|jefferson"), "squat"),
    (_rx(r"\brow\b|rows\b|band pull"), "horizontal_pull"),
    (_rx(r"bench press|push up|chest press|floor press|board press|pin press|spoto|\bdip\b|dips\b|guillotine|chain press|planche|jm press|body up|close grip press|drop push"), "horizontal_push"),
    (_rx(r"crunch|sit up|plank|side bridge|leg raise|knee raise|rollout|roll out|fallout|dead bug|hollow|v-up|v-sit|l-sit|flutter|mountain climber|side bend|jackknife|ab roller|ab wheel|pallof|bird dog|toes to bar|knees to elbow|leg pull in|heel touch|toe touch|tuck|bottoms up|butt up|cocoon|elbow to knee|pike|dragon flag|stomach vacuum|knee/hip raise|scissor kick|spider crawl|copenhagen|otis up|janda|press sit up|gorilla|turkish get up|superman|arch hold|windmill|figure 8|balance"), "core"),
]
CROSSFIT_PATTERNS = [
    r"thruster", r"wall ball", r"burpee", r"double under", r"toes to bar", r"knees to elbow", r"\bkipping\b",
    r"butterfly pull up", r"muscle up", r"handstand", r"rope climb", r"devil press", r"man maker", r"box jump",
    r"american kettlebell swing", r"^kettlebell swing$", r"\bcluster\b", r"\bghd\b", r"ski erg", r"air bike",
    r"^rowing machine$", r"chest to bar", r"wall walk", r"sumo deadlift high pull", r"^pistol squat$",
    r"^overhead squat \(barbell\)$", r"^(hang )?power (clean|snatch) \(barbell\)$", r"^(push|split) jerk \(barbell\)$",
    r"^push press \(barbell\)$", r"^deadlift \(barbell\)$", r"^front squat \(barbell\)$", r"^jump rope$",
    r"^sled push$", r"sandbag", r"^ring dip$", r"^pull up$", r"^push up$", r"^air squat$|^bodyweight squat$",
]
CROSSFIT_RE = _rx("|".join(f"(?:{p})" for p in CROSSFIT_PATTERNS))
# Base names (before the qualifier and equipment) that are powerlifting lifts when done with a barbell.
POWERLIFTING_BASES = {
    "squat", "bench press", "deadlift", "sumo deadlift", "box squat", "pause squat", "pin squat",
    "paused bench press", "deficit deadlift", "block pull", "rack pull", "board press", "floor press",
    "spoto press", "pin press", "good morning", "low bar squat", "powerlifting bench press",
    "competition bench press", "larsen press", "anderson squat", "safety bar squat",
}
GYM_EQUIPMENT = {
    "barbell", "dumbbell", "cable", "machine", "smith_machine", "ez_bar", "trap_bar",
    "resistance_band", "plate", "landmine", "bodyweight", "pull_up_bar", "stability_ball", "other",
}

# (regex on the core name, family key); first match wins.
FAMILY_RULES = [
    (_rx(r"romanian deadlift|stiff leg deadlift|\brdl\b"), "romanian-deadlift"),
    (_rx(r"sumo deadlift high pull"), "sumo-deadlift-high-pull"),
    (_rx(r"deadlift|rack pull|block pull"), "deadlift"),
    (_rx(r"hip thrust"), "hip-thrust"),
    (_rx(r"glute bridge|hip bridge|butt lift"), "glute-bridge"),
    (_rx(r"good morning"), "good-morning"),
    (_rx(r"bench press|board press|pin press|spoto press|guillotine"), "bench-press"),
    (_rx(r"floor press"), "floor-press"),
    (_rx(r"chest press"), "chest-press"),
    (_rx(r"handstand push up"), "handstand-push-up"),
    (_rx(r"push up"), "push-up"),
    (_rx(r"(?<!jerk )\bdip\b"), "dip"),
    (_rx(r"reverse fly|rear delt fly|rear delt raise|rear lateral raise"), "reverse-fly"),
    (_rx(r"chest fly|cable crossover|\bfly\b"), "chest-fly"),
    (_rx(r"arnold press"), "arnold-press"),
    (_rx(r"push press"), "push-press"),
    (_rx(r"overhead press|shoulder press|military press"), "overhead-press"),
    (_rx(r"bradford press"), "bradford-press"),
    (_rx(r"clean and jerk"), "clean-and-jerk"),
    (_rx(r"clean and press"), "clean-and-press"),
    (_rx(r"\bjerk\b"), "jerk"),
    (_rx(r"clean pull|clean shrug"), "clean-pull"),
    (_rx(r"snatch pull|snatch shrug"), "snatch-pull"),
    (_rx(r"snatch balance"), "snatch-balance"),
    (_rx(r"\bsnatch\b"), "snatch"),
    (_rx(r"\bclean\b"), "clean"),
    (_rx(r"thruster"), "thruster"),
    (_rx(r"calf raise|calf press"), "calf-raise"),
    (_rx(r"front squat"), "front-squat"),
    (_rx(r"goblet squat"), "goblet-squat"),
    (_rx(r"overhead squat"), "overhead-squat"),
    (_rx(r"hack squat"), "hack-squat"),
    (_rx(r"bulgarian split squat"), "bulgarian-split-squat"),
    (_rx(r"split squat"), "split-squat"),
    (_rx(r"pistol"), "pistol-squat"),
    (_rx(r"jump squat|squat jump"), "jump-squat"),
    (_rx(r"sissy squat"), "sissy-squat"),
    (_rx(r"zercher"), "zercher-squat"),
    (_rx(r"belt squat"), "belt-squat"),
    (_rx(r"\bsquat\b"), "squat"),
    (_rx(r"leg press"), "leg-press"),
    (_rx(r"lunge"), "lunge"),
    (_rx(r"step up"), "step-up"),
    (_rx(r"nordic"), "nordic-hamstring-curl"),
    (_rx(r"leg curl|hamstring curl"), "leg-curl"),
    (_rx(r"leg extension"), "leg-extension"),
    (_rx(r"hammer curl"), "hammer-curl"),
    (_rx(r"preacher"), "preacher-curl"),
    (_rx(r"concentration curl"), "concentration-curl"),
    (_rx(r"wrist curl|finger curl"), "wrist-curl"),
    (_rx(r"reverse curl"), "reverse-curl"),
    (_rx(r"zottman"), "zottman-curl"),
    (_rx(r"spider curl"), "spider-curl"),
    (_rx(r"\bcurl\b"), "bicep-curl"),
    (_rx(r"triceps extension|skull crusher|jm press|tate press"), "triceps-extension"),
    (_rx(r"pushdown"), "triceps-pushdown"),
    (_rx(r"triceps kickback"), "triceps-kickback"),
    (_rx(r"lateral raise"), "lateral-raise"),
    (_rx(r"front raise"), "front-raise"),
    (_rx(r"upright row"), "upright-row"),
    (_rx(r"shrug"), "shrug"),
    (_rx(r"straight arm pulldown"), "straight-arm-pulldown"),
    (_rx(r"pulldown"), "lat-pulldown"),
    (_rx(r"muscle up"), "muscle-up"),
    (_rx(r"pull up|chin up"), "pull-up"),
    (_rx(r"inverted row"), "inverted-row"),
    (_rx(r"t-bar row|landmine row"), "t-bar-row"),
    (_rx(r"seated row|cable row"), "seated-row"),
    (_rx(r"upright row"), "upright-row"),
    (_rx(r"\brow\b"), "row"),
    (_rx(r"face pull"), "face-pull"),
    (_rx(r"pull apart"), "band-pull-apart"),
    (_rx(r"side plank"), "side-plank"),
    (_rx(r"plank"), "plank"),
    (_rx(r"crunch"), "crunch"),
    (_rx(r"sit up"), "sit-up"),
    (_rx(r"leg raise|knee raise"), "leg-raise"),
    (_rx(r"rollout"), "ab-rollout"),
    (_rx(r"russian twist"), "russian-twist"),
    (_rx(r"wood ?chop|cable lift"), "wood-chop"),
    (_rx(r"pallof"), "pallof-press"),
    (_rx(r"side bend"), "side-bend"),
    (_rx(r"kettlebell swing"), "kettlebell-swing"),
    (_rx(r"turkish get up"), "turkish-get-up"),
    (_rx(r"windmill"), "windmill"),
    (_rx(r"burpee"), "burpee"),
    (_rx(r"box jump"), "box-jump"),
    (_rx(r"reverse hyperextension"), "reverse-hyperextension"),
    (_rx(r"hyperextension|back extension"), "back-extension"),
    (_rx(r"glute ham raise"), "glute-ham-raise"),
    (_rx(r"foam roll"), "foam-roll"),
    (_rx(r"external rotation|internal rotation"), "shoulder-rotation"),
    (_rx(r"atlas stone"), "atlas-stone"),
    (_rx(r"\bhang\b"), "dead-hang"),
]
FAMILY_STRIP_PREFIXES = re.compile(
    r"^(?:(?:incline|decline|seated|standing|lying|kneeling|single arm|single leg|alternating|"
    r"close grip|wide grip|reverse grip|neutral grip|wide stance|narrow stance|weighted|speed|"
    r"pause|paused|deficit|banded|double|two arm|flat bench|lever|prone|supine|elevated|"
    r"half kneeling|tall kneeling|bent over|low|high|kipping|strict|assisted|band assisted|partner|"
    r"isometric|tempo|hanging)\s+)+",
    re.I,
)


def core_name(name: str) -> str:
    base, _, _ = split_name(name)
    stripped = FAMILY_STRIP_PREFIXES.sub("", base)
    return stripped or base


STRETCH_TARGETS = [
    ("hip flexor", "hip-flexor"), ("quad", "quad"), ("hamstring", "hamstring"), ("calf", "calf"),
    ("gastrocnemius", "calf"), ("soleus", "calf"), ("glute", "glute"), ("piriformis", "glute"),
    ("groin", "groin"), ("adductor", "groin"), ("chest", "chest"), ("pec", "chest"),
    ("triceps", "triceps"), ("lat", "lat"), ("bicep", "bicep"), ("shoulder", "shoulder"),
    ("deltoid", "shoulder"), ("neck", "neck"), ("back", "back"),
]
STRETCH_TARGET_RE = re.compile(r"\b(" + "|".join(t for t, _ in STRETCH_TARGETS) + r")\b", re.I)


def derive_family(name: str) -> str:
    base, _, _ = split_name(name)
    if re.search(r"\bstretch\b", base, re.I):
        m = STRETCH_TARGET_RE.search(base)
        if m:
            return dict(STRETCH_TARGETS)[m.group(1).lower()] + "-stretch"
    for rx, key in FAMILY_RULES:
        if rx.search(base):
            return key
    return slugify(core_name(name))


def derive_pattern(ex: dict) -> str:
    name = ex["name"]
    cat = ex["category"]
    if cat in ("stretching", "mobility"):
        return "mobility"
    if cat == "cardio":
        return "locomotion"
    base_part, _, label = split_name(name)
    probe = f"{base_part} ({label})" if label else base_part
    for rx, pattern in PATTERN_RULES:
        if rx.search(probe):
            return pattern
    if cat == "plyometric":
        return "explosive"
    if ex.get("mechanic") == "isolation":
        primary = ex["primary_muscles"][0]
        if primary in ("abdominals", "obliques"):
            return "core"
        return "isolation"
    primary = ex["primary_muscles"][0]
    force = ex.get("force")
    by_muscle = {
        "chest": "horizontal_push", "triceps": "horizontal_push", "lats": "vertical_pull",
        "upper_back": "horizontal_pull", "traps": "horizontal_pull", "quadriceps": "squat",
        "hamstrings": "hinge", "glutes": "hinge", "lower_back": "hinge", "abdominals": "core",
        "obliques": "rotation", "hip_flexors": "core", "biceps": "isolation", "forearms": "isolation",
        "calves": "isolation", "tibialis": "isolation", "neck": "isolation", "adductors": "isolation",
        "abductors": "isolation", "full_body": "explosive",
    }
    if primary == "shoulders":
        return "vertical_push" if force == "push" else "isolation"
    return by_muscle.get(primary, "isolation")


def derive_disciplines(ex: dict, base_discipline: str | None) -> list[str]:
    found = set()
    if base_discipline:
        found.add(base_discipline)
    cat, eq, name = ex["category"], ex["equipment"], ex["name"]
    if cat in ("stretching", "mobility"):
        found.add("mobility")
    if cat == "cardio":
        found.add("cardio")
    if cat == "plyometric":
        found.add("functional")
    if cat == "olympic":
        found.add("olympic_weightlifting")
    if eq in ("kettlebell", "suspension_trainer", "medicine_ball", "sandbag", "sled"):
        found.add("functional")
    if eq in ("bodyweight", "pull_up_bar", "rings") and cat in ("strength", "isometric"):
        found.add("calisthenics")
    if CROSSFIT_RE.search(name):
        found.add("crossfit")
    base_part = re.sub(r"\s+with (bands|chains)$", "", split_name(name)[0], flags=re.I).lower()
    if base_part in POWERLIFTING_BASES and eq == "barbell":
        found.add("powerlifting")
    if cat in ("strength", "isometric") and eq in GYM_EQUIPMENT and not found & {"strongman", "olympic_weightlifting"}:
        found.add("bodybuilding")
    if not found:
        found.add("bodybuilding")
    return [d for d in V.DISCIPLINES if d in found]


OBLIQUE_RE = _rx(r"oblique|side bend|russian twist|\btwist\b|side plank|side bridge|wood ?chop|windmill|side jackknife|landmine 180|judo flip|cable lift|windshield wiper|heel touch|cross body crunch|bicycle crunch|cross-body")
HIP_FLEXOR_RE = _rx(r"leg raise|knee raise|hanging pike|toes to bar|knees to elbow|leg pull in|flutter kick|scissor kick|mountain climber|l-sit|v-up|v-sit|jackknife|knee/hip raise|tuck crunch|hip flexion")


def refine_muscles(ex: dict) -> None:
    """Move oblique work to obliques and add hip flexors to leg-raise style moves."""
    name = ex["name"]
    prim, sec = ex["primary_muscles"], ex["secondary_muscles"]
    if OBLIQUE_RE.search(name) and "abdominals" in prim and "obliques" not in prim:
        prim[prim.index("abdominals")] = "obliques"
        if "abdominals" not in sec:
            sec.insert(0, "abdominals")
    if HIP_FLEXOR_RE.search(name) and "hip_flexors" not in prim and "hip_flexors" not in sec:
        sec.append("hip_flexors")
    ex["secondary_muscles"] = [m for m in dict.fromkeys(sec) if m not in prim]
    ex["primary_muscles"] = list(dict.fromkeys(prim))


def derive_category(ex: dict) -> str:
    cat, name = ex["category"], ex["name"]
    if cat == "stretching" and re.search(r"foam roll|\bsmr\b|circle|swing|rotation|inchworm|groiner|windmill|hop|march|claw", name, re.I):
        return "mobility"
    return cat


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def read_json(path: Path, what: str):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise BuildError(f"{what} not found: {path}")
    except json.JSONDecodeError as exc:
        raise BuildError(f"{what} is not valid JSON ({path}): {exc}")


def _str_list(value, field, where):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BuildError(f"{where}: {field} must be a list of strings")
    return [v.strip() for v in value if v.strip()]


def load_base(path: Path) -> list[dict]:
    data = read_json(path, "base exercises file")
    if not isinstance(data, list):
        raise BuildError("base file must be a JSON list of exercises")
    out = []
    seen_ids = set()
    for i, raw in enumerate(data):
        where = f"base[{i}]"
        if not isinstance(raw, dict):
            raise BuildError(f"{where}: not an object")
        ident, name = raw.get("id"), raw.get("name")
        if not isinstance(ident, str) or not ident or not isinstance(name, str) or not name.strip():
            raise BuildError(f"{where}: missing id or name")
        if ident in seen_ids:
            raise BuildError(f"{where}: duplicate id {ident!r}")
        seen_ids.add(ident)
        for field, mapping in (("equipment", BASE_EQUIPMENT), ("category", BASE_CATEGORY), ("level", BASE_LEVEL),
                               ("force", BASE_FORCE), ("mechanic", BASE_MECHANIC)):
            if raw.get(field) not in mapping:
                raise BuildError(f"{where} ({ident}): unknown {field} {raw.get(field)!r}")
        muscles = {}
        for field in ("primaryMuscles", "secondaryMuscles"):
            values = _str_list(raw.get(field), field, where)
            for m in values:
                if m not in BASE_MUSCLE:
                    raise BuildError(f"{where} ({ident}): unknown muscle {m!r}")
            muscles[field] = [BASE_MUSCLE[m] for m in values]
        out.append({
            "id": ident,
            "name": re.sub(r"\s+", " ", name).strip(),
            "equipment": raw.get("equipment"),
            "category": raw.get("category"),
            "level": raw.get("level"),
            "force": raw.get("force"),
            "mechanic": raw.get("mechanic"),
            "primary": muscles["primaryMuscles"],
            "secondary": muscles["secondaryMuscles"],
            "instructions": _str_list(raw.get("instructions"), "instructions", where),
        })
    return out


def base_equipment(b: dict, equipment_fix: dict) -> str:
    """Vocabulary equipment for a base entry: curated fix, else the base value refined by the name."""
    if b["id"] in equipment_fix:
        return equipment_fix[b["id"]]
    low = b["name"].lower()
    if "smith" in low:
        return "smith_machine"
    if re.search(r"\be-?z[- ]?(curl )?bar\b", low):
        return "ez_bar"
    if "trap bar" in low:
        return "trap_bar"
    return BASE_EQUIPMENT[b["equipment"]]


def _check_keys(obj, allowed, where):
    unknown = sorted(k for k in obj if k not in allowed and not k.startswith("_"))
    if unknown:
        raise BuildError(f"{where}: unknown keys {unknown}")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

OVERRIDE_FIELDS = set(V.EXERCISE_KEYS) - {"slug", "name"}
OVERRIDE_EXTRA = {"aliases_add", "aliases_remove", "disciplines_add", "disciplines_remove", "secondary_muscles_add"}
SUPPLEMENT_REQUIRED = {"name", "category", "equipment", "primary_muscles", "level", "instructions"}

# Alias priorities: higher wins when two exercises claim the same alias.
P_CURATED, P_ORIGINAL, P_MERGED, P_GENERATED = 3, 2, 1, 0


def build(base_path: Path, out_path: Path, review_tsv: Path | None, library_version: str) -> dict:
    warnings: list[str] = []
    base = load_base(base_path)
    by_id = {b["id"]: b for b in base}

    curation = read_json(HERE / "base_curation.json", "base_curation.json")
    _check_keys(curation, {"drop", "merge", "rename", "equipment", "category"}, "base_curation.json")
    drops = curation.get("drop", {})
    merges = curation.get("merge", {})
    renames = curation.get("rename", {})
    equipment_fix = curation.get("equipment", {})
    category_fix = curation.get("category", {})
    for section, table in (("drop", drops), ("merge", merges), ("rename", renames), ("equipment", equipment_fix), ("category", category_fix)):
        for ident in table:
            if ident.startswith("_"):
                continue
            if ident not in by_id:
                raise BuildError(f"base_curation.json {section}: unknown base id {ident!r}")
    for src, dst in merges.items():
        if src.startswith("_"):
            continue
        if dst not in by_id or dst in merges or dst in drops:
            raise BuildError(f"base_curation.json merge: {src!r} -> {dst!r} must target a kept base id")
    for ident, eq in equipment_fix.items():
        if not ident.startswith("_") and eq not in V.EQUIPMENT:
            raise BuildError(f"base_curation.json equipment: {ident!r} -> {eq!r} is not in the vocabulary")
    for ident, cat in category_fix.items():
        if not ident.startswith("_") and cat not in V.CATEGORIES:
            raise BuildError(f"base_curation.json category: {ident!r} -> {cat!r} is not in the vocabulary")

    # alias candidates: slug -> list of (alias, priority)
    alias_candidates: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    exercises: dict[str, dict] = {}
    origin: dict[str, str] = {}
    id_to_slug: dict[str, str] = {}

    collisions: list[str] = []

    def add_exercise(ex: dict, source: str):
        slug = slugify(ex["name"])
        if not slug:
            raise BuildError(f"{source}: name {ex['name']!r} gives an empty slug")
        if slug in exercises:
            collisions.append(f"{source}: {ex['name']!r} collides with {exercises[slug]['name']!r} ({origin[slug]})")
            slug = f"{slug}-collision-{len(collisions)}"
        ex["slug"] = slug
        exercises[slug] = ex
        origin[slug] = source
        return slug

    # 1. base entries
    for b in base:
        ident = b["id"]
        if ident in drops or ident in merges:
            continue
        equipment = base_equipment(b, equipment_fix)
        name = renames.get(ident) or rule_rename(b["name"], equipment)
        category, base_disc = BASE_CATEGORY[b["category"]]
        if ident in category_fix:
            category = category_fix[ident]
        ex = {
            "name": name,
            "category": category,
            "equipment": equipment,
            "primary_muscles": list(b["primary"]),
            "secondary_muscles": list(b["secondary"]),
            "mechanic": BASE_MECHANIC[b["mechanic"]],
            "force": BASE_FORCE[b["force"]],
            "level": BASE_LEVEL[b["level"]],
            "instructions": clean_instructions(b["instructions"]),
            "_base_discipline": base_disc,
            "_base_id": ident,
        }
        if not ex["primary_muscles"]:
            raise BuildError(f"base {ident}: no primary muscle")
        ex["category"] = derive_category(ex)
        slug = add_exercise(ex, f"base:{ident}")
        id_to_slug[ident] = slug
        alias_candidates[slug].append((b["name"], P_ORIGINAL))

    merge_count = 0
    for src, dst in sorted(merges.items()):
        if src.startswith("_"):
            continue
        slug = id_to_slug[dst]
        sb = by_id[src]
        alias_candidates[slug].append((sb["name"], P_MERGED))
        alias_candidates[slug].append((rule_rename(sb["name"], base_equipment(sb, equipment_fix)), P_MERGED))
        target = exercises[slug]
        if not target["instructions"]:
            target["instructions"] = clean_instructions(sb["instructions"])
        merge_count += 1

    # 2. derived fields
    for ex in exercises.values():
        if ex["category"] in ("stretching", "mobility"):
            # mechanic and push/pull are strength concepts; stretches are held, drills are dynamic.
            ex["mechanic"] = None
            ex["force"] = "static" if ex["category"] == "stretching" else None
        refine_muscles(ex)
        ex["movement_pattern"] = derive_pattern(ex)
        ex["disciplines"] = derive_disciplines(ex, ex.pop("_base_discipline"))
        ex["family"] = derive_family(ex["name"])

    # 3. overrides (keyed by final slug)
    overrides = read_json(HERE / "overrides.json", "overrides.json")
    if not isinstance(overrides, dict):
        raise BuildError("overrides.json must be an object keyed by slug")
    supplement = read_json(HERE / "supplement.json", "supplement.json")
    if isinstance(supplement, dict):
        supplement = supplement.get("exercises")
    if not isinstance(supplement, list):
        raise BuildError("supplement.json must be a list (or an object with an 'exercises' list)")

    # 4. supplement entries
    supplement_count = 0
    for i, raw in enumerate(supplement):
        where = f"supplement[{i}]"
        if not isinstance(raw, dict):
            raise BuildError(f"{where}: must be an object")
        _check_keys(raw, set(V.EXERCISE_KEYS) - {"slug"}, where)
        missing = sorted(SUPPLEMENT_REQUIRED - raw.keys())
        if missing:
            raise BuildError(f"{where} ({raw.get('name')}): missing {missing}")
        steps = _str_list(raw["instructions"], "instructions", where)
        if not 3 <= len(steps) <= 6:
            warnings.append(f"{where} ({raw['name']}): {len(steps)} instruction steps (aim for 3-6)")
        ex = {
            "name": raw["name"].strip(),
            "category": raw["category"],
            "equipment": raw["equipment"],
            "primary_muscles": _str_list(raw["primary_muscles"], "primary_muscles", where),
            "secondary_muscles": _str_list(raw.get("secondary_muscles"), "secondary_muscles", where),
            "mechanic": raw.get("mechanic"),
            "force": raw.get("force"),
            "level": raw["level"],
            "instructions": steps,
        }
        ex["movement_pattern"] = raw.get("movement_pattern") or derive_pattern(ex)
        ex["disciplines"] = raw.get("disciplines") or derive_disciplines(ex, None)
        ex["family"] = raw.get("family") or derive_family(ex["name"])
        slug = add_exercise(ex, where)
        for alias in _str_list(raw.get("aliases"), "aliases", where):
            alias_candidates[slug].append((alias, P_CURATED))
        supplement_count += 1

    if collisions:
        raise BuildError("name collisions (merge, drop or rename one of each pair):\n  " + "\n  ".join(collisions))

    # 5. apply overrides
    unknown = sorted(k for k in overrides if not k.startswith("_") and k not in exercises)
    if unknown:
        raise BuildError("overrides.json: no exercise with these slugs (renamed or removed?): " + ", ".join(unknown))
    for slug, patch in sorted(overrides.items()):
        if slug.startswith("_"):
            continue
        if not isinstance(patch, dict):
            raise BuildError(f"overrides.json[{slug}]: must be an object")
        _check_keys(patch, OVERRIDE_FIELDS | OVERRIDE_EXTRA, f"overrides.json[{slug}]")
        ex = exercises[slug]
        for field, value in patch.items():
            if field.startswith("_"):
                continue
            if field == "aliases":
                alias_candidates[slug] = [(a, P_CURATED) for a in _str_list(value, field, slug)]
                ex["_aliases_replaced"] = True
            elif field == "aliases_add":
                alias_candidates[slug].extend((a, P_CURATED) for a in _str_list(value, field, slug))
            elif field == "aliases_remove":
                drop_keys = {normalize_name(a) for a in _str_list(value, field, slug)}
                alias_candidates[slug] = [(a, p) for a, p in alias_candidates[slug] if normalize_name(a) not in drop_keys]
                ex.setdefault("_alias_blocklist", set()).update(drop_keys)
            elif field == "disciplines_add":
                ex["disciplines"] = list(dict.fromkeys(ex["disciplines"] + _str_list(value, field, slug)))
            elif field == "disciplines_remove":
                ex["disciplines"] = [d for d in ex["disciplines"] if d not in set(_str_list(value, field, slug))]
            elif field == "secondary_muscles_add":
                ex["secondary_muscles"] = list(dict.fromkeys(ex["secondary_muscles"] + _str_list(value, field, slug)))
            elif field == "instructions":
                ex["instructions"] = clean_instructions(_str_list(value, field, slug))
            else:
                ex[field] = value
        ex["secondary_muscles"] = [m for m in ex["secondary_muscles"] if m not in ex["primary_muscles"]]
        ex["disciplines"] = [d for d in V.DISCIPLINES if d in set(ex["disciplines"])] + [
            d for d in ex["disciplines"] if d not in V.DISCIPLINES]

    # 6. generated aliases ("Bench Press (Barbell)" -> "Barbell Bench Press")
    for slug, ex in exercises.items():
        if ex.get("_aliases_replaced"):
            continue
        base_part, qual, label = split_name(ex["name"])
        if label and label in EQUIPMENT_LABELS.values():
            words = [qual] if qual and not re.search(r",", qual) else []
            alias_candidates[slug].append((" ".join(words + [label, base_part]), P_GENERATED))

    # 7. families: a family with a single member uses that member's slug
    fam_members = collections.Counter(ex["family"] for ex in exercises.values())
    for slug, ex in exercises.items():
        if fam_members[ex["family"]] == 1 and ex["family"] != slug:
            ex["family"] = slug

    # 8. alias resolution
    name_keys = {normalize_name(ex["name"]): slug for slug, ex in exercises.items()}
    claims: dict[str, list[tuple[int, str, str]]] = collections.defaultdict(list)
    for slug, cands in alias_candidates.items():
        ex = exercises[slug]
        own = normalize_name(ex["name"])
        block = ex.get("_alias_blocklist", set())
        best: dict[str, tuple[int, str]] = {}
        for alias, prio in cands:
            alias = re.sub(r"\s+", " ", alias).strip()
            key = normalize_name(alias)
            if not key or key == own or key in block:
                continue
            if key in name_keys and name_keys[key] != slug:
                if prio == P_CURATED:
                    raise BuildError(f"{slug}: curated alias {alias!r} is the name of {name_keys[key]}")
                warnings.append(f"alias {alias!r} dropped from {slug}: it is the name of {name_keys[key]}")
                continue
            if key not in best or prio > best[key][0]:
                best[key] = (prio, alias)
        for key, (prio, alias) in best.items():
            claims[key].append((prio, slug, alias))
    final_aliases: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for key, owners in claims.items():
        owners.sort(key=lambda t: (-t[0], t[1]))
        top = owners[0][0]
        winners = [o for o in owners if o[0] == top]
        if len(winners) > 1:
            if top == P_CURATED:
                raise BuildError(f"curated alias {winners[0][2]!r} is claimed by {[w[1] for w in winners]}")
            warnings.append(f"alias {winners[0][2]!r} dropped: claimed by {[w[1] for w in winners]}")
            continue
        prio, slug, alias = winners[0]
        for loser in owners[1:]:
            warnings.append(f"alias {loser[2]!r} kept on {slug}, dropped from {loser[1]}")
        final_aliases[slug].append((prio, alias))

    # 9. assemble records in schema order
    records = []
    for slug, ex in exercises.items():
        aliases = [a for _, a in sorted(final_aliases.get(slug, []), key=lambda t: (-t[0], t[1].casefold()))]
        records.append({
            "slug": slug,
            "name": ex["name"],
            "aliases": aliases,
            "category": ex["category"],
            "equipment": ex["equipment"],
            "primary_muscles": ex["primary_muscles"],
            "secondary_muscles": ex["secondary_muscles"],
            "movement_pattern": ex["movement_pattern"],
            "mechanic": ex["mechanic"],
            "force": ex["force"],
            "level": ex["level"],
            "disciplines": ex["disciplines"],
            "family": ex["family"],
            "instructions": ex["instructions"],
        })
    records.sort(key=V.sort_key)

    doc = {
        "schema_version": V.SCHEMA_VERSION,
        "library_version": library_version,
        "sources": SOURCES,
        "exercises": records,
    }

    # QA warnings: names that differ only by a plural, and equipment suffix gaps.
    plural_keys = collections.defaultdict(list)
    for rec in records:
        key = " ".join(re.sub(r"(?<=[a-z])e?s$", "", w) for w in re.findall(r"[a-z0-9]+", rec["name"].lower()))
        plural_keys[key].append(rec["slug"])
    for key, slugs in plural_keys.items():
        if len(slugs) > 1:
            warnings.append(f"near-duplicate names: {slugs}")
    for rec in records:
        label = EQUIPMENT_LABELS.get(rec["equipment"])
        if label and rec["equipment"] != "plate" and not any(
                re.search(rf"\b{re.escape(w)}\b", rec["name"], re.I) for w in label.split()):
            warnings.append(f"name lacks equipment label ({label}): {rec['name']}")
        if "rehab" in rec["disciplines"] and rec["level"] != "beginner":
            warnings.append(f"rehab exercise is not beginner level: {rec['name']} ({rec['level']})")

    if review_tsv:
        with review_tsv.open("w", encoding="utf-8") as fh:
            fh.write("slug\tname\tsource\tcategory\tequipment\tpattern\tfamily\tdisciplines\tprimary\tsecondary\tlevel\taliases\n")
            for rec in records:
                fh.write("\t".join([
                    rec["slug"], rec["name"], origin[rec["slug"]], rec["category"], rec["equipment"],
                    rec["movement_pattern"], rec["family"], ",".join(rec["disciplines"]),
                    ",".join(rec["primary_muscles"]), ",".join(rec["secondary_muscles"]), rec["level"],
                    " | ".join(rec["aliases"]),
                ]) + "\n")

    errors = V.validate(doc)
    if errors:
        raise BuildError("validation failed:\n  " + "\n  ".join(errors[:100]))

    write_atomic(out_path, doc)
    drop_count = sum(1 for k in drops if not k.startswith("_"))
    print(f"Wrote {len(records)} exercises to {out_path}")
    print(f"  base kept: {len(records) - supplement_count}, supplement: {supplement_count}, "
          f"merged: {merge_count}, dropped: {drop_count}")
    if warnings:
        print(f"  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"    - {w}")
    return doc


def write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Rytivo exercise library JSON.")
    parser.add_argument("--base", required=True, type=Path, help="path to free-exercise-db dist/exercises.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output path (default {DEFAULT_OUT.relative_to(REPO_ROOT)})")
    parser.add_argument("--review-tsv", type=Path, help="also write a tab-separated review table here")
    parser.add_argument("--library-version", default=LIBRARY_VERSION, help=f"library_version to stamp (default {LIBRARY_VERSION})")
    args = parser.parse_args(argv)
    try:
        build(args.base, args.out, args.review_tsv, args.library_version)
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
