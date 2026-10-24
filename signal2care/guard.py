"""Deterministic check of a drafted chat reply before it is sent. Narrow on purpose.

General conversation is left alone. Only two health patterns are caught: telling someone what to do
with a medicine (or giving a dose), and stating that the person has a named condition. The patterns
are conservative, so a normal explanation ("ibuprofen is an anti-inflammatory") passes.
"""
from __future__ import annotations

import re

_DRUGS = (r"ibuprofen|advil|motrin|tylenol|acetaminophen|paracetamol|aspirin|naproxen|aleve|insulin|metformin|statin|"
          r"beta[- ]?blocker|antibiotic|amoxicillin|prednisone|antidepressant|ssri|benzodiazepine|xanax|opioid|oxycodone|"
          r"melatonin|supplement|pills?|tablets?|medication|medicine|meds|dose|dosage")
_DIRECTIVE = re.compile(
    rf"\b(you (should|could|can|must|need to|ought to)|try|consider|go ahead and|i('d| would) (suggest|recommend)|just)\b"
    rf"[^.?!\n]{{0,40}}\b(take|taking|start|starting|stop|stopping|skip|skipping|increase|increasing|decrease|double|halve|"
    rf"cut|adjust|switch)\b[^.?!\n]{{0,50}}\b({_DRUGS})\b", re.I)
_DOSE = re.compile(r"\b\d+(\.\d+)?\s?(mg|mcg|µg|milligrams?|micrograms?|iu)\b", re.I)
_DIAGNOSIS = re.compile(
    r"\b(you (have|likely have|probably have|definitely have|may have got|'ve got|have got|are suffering from)|"
    r"this (is|sounds like|looks like|means you have)|it('s| is) (probably|likely|definitely)|you('re| are) (probably|likely) (getting|coming down with))\b"
    r"[^.?!\n]{0,40}\b(infection|flu|influenza|covid|coronavirus|virus|viral|diabetes|hypertension|high blood pressure|"
    r"arrhythmia|afib|atrial fibrillation|sleep apnea|apnea|anemia|cancer|pneumonia|sepsis|asthma|heart (disease|condition|problem)|"
    r"thyroid|depression|anxiety disorder)\b", re.I)

REWRITE_NOTE = (
    "Your draft either told the person what to do with a medicine, gave a dose, or said they have a named condition. "
    "Rewrite it. You may describe what the data show and share general, well-established information, but do not "
    "advise on medicines or doses and do not say or imply a diagnosis. If it matters, suggest talking with a clinician "
    "or pharmacist. Keep the same friendly, short style."
)
FALLBACKS = {
    "diagnosis": "I can't diagnose anything or say what a reading or symptom means for your health. I can walk through what "
                 "your data actually show, or help you write down questions for a clinician.",
    "medicine": "I can't advise on medicines or doses. A doctor or pharmacist can tell you what is safe for you. I can still "
                "look at your data with you or help you prepare questions for them.",
}

# A refusal that mentions the condition ("I can't say whether you have the flu") is exactly what we want.
_NEGATION = re.compile(r"can'?t|cannot|can not|couldn'?t|won'?t|unable|not able|don'?t know|do not know|whether|\bif\b|"
                       r"isn'?t|not a (doctor|clinician)|no way to|rule out|not (say|tell|diagnos)", re.I)


def _asserted(pattern: re.Pattern, text: str) -> bool:
    for m in pattern.finditer(text):
        window = text[max(0, m.start() - 60):m.end()]
        if not _NEGATION.search(window):
            return True
    return False


def violations(text: str) -> list[str]:
    found = []
    if _asserted(_DIRECTIVE, text):
        found.append("medicine_directive")
    if _DOSE.search(text) and re.search(rf"\b({_DRUGS})\b", text, re.I) and not _NEGATION.search(text):
        found.append("dose")
    if _asserted(_DIAGNOSIS, text):
        found.append("diagnosis")
    return found


def fallback_for(found: list[str]) -> str:
    return FALLBACKS["diagnosis"] if found == ["diagnosis"] else FALLBACKS["medicine"]
