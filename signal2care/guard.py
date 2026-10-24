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


# ---------------------------------------------------------------------------------------------------------------
# Evidence discipline. The assistant may state three kinds of things about health: measurements from the wearable (read
# by tools), arithmetic on the person's own history (computed by code), and what published studies report, with a PubMed id
# and the kind of study. Anything else about a cause or a meaning is a guess, and a guess is not sent.
EVIDENCE_RULES = (
    "Say 'studies report' or name the kind of study (for example 'a systematic review'), and cite the PubMed id like (PMID 12345). "
    "Repeat only what is in key_sentences, with the same numbers. Say this is general background and does not explain this person's own "
    "reading. If the studies do not answer the question, say so plainly. Do not fill gaps from memory."
)
_STUDY_WORDS = re.compile(r"\b(stud(y|ies)|research(ers)?|papers?|trials?|meta-analys[ie]s|systematic review|according to|evidence (shows|suggests|indicates)|scientists?)\b", re.I)
_PMID = re.compile(r"PMID[:\s]*(\d{6,9})", re.I)
_SPECULATE = re.compile(
    r"\b(probably|likely|might be|may be|could be|possibly|perhaps|maybe)\b[^.?!\n]{0,50}\b(because|due to|from|caused|a sign|signs?|related to|linked to|result of|sign of|means)\b"
    r"|\b(can|could|may|might) (go along with|come from|point to|be a sign|be caused|mean)\b|\bsuggests (you|that you)\b|\bthat (makes sense|would explain)\b",
    re.I)
_CAUSAL = re.compile(r"\b(this|that|it)('s| is| was) (because|due to|caused by|from your)\b|\bbecause of your\b|\bis caused by\b|\bis due to your\b", re.I)
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


def _numbers(text: str) -> list[float]:
    return [float(x) for x in _NUM.findall(text)]


def unsupported_numbers(reply: str, allowed_text: str, user_text: str = "") -> list[str]:
    """Numbers in a reply that appear nowhere in the data we gave the model (tool results, the open alert) or in the person's message.

    Small counts (0-12), clock times and years are ignored. Rounding is allowed: within 1.5 percent or 0.6 of a source number.
    """
    pool = _numbers(allowed_text) + _numbers(user_text)
    bad = []
    cleaned = re.sub(r"\b\d{1,2}:\d{2}\b|\b20\d{2}\b|\b911\b|\b988\b", " ", reply)
    for tok in _NUM.findall(cleaned):
        v = float(tok)
        if v <= 12 and float(v).is_integer():
            continue
        if any(abs(v - p) <= max(0.6, 0.015 * abs(p)) for p in pool):
            continue
        bad.append(tok)
    return bad[:5]


def grounding_problems(reply: str, *, trace_text: str, used_evidence: bool, data_context: bool, user_text: str = "") -> list[str]:
    """Why a reply makes claims it cannot back. Empty means it is fine to send."""
    problems: list[str] = []
    if _STUDY_WORDS.search(reply) and not used_evidence:
        problems.append("study_without_source")
    cited = set(_PMID.findall(reply))
    if cited and not all(p in trace_text for p in cited):
        problems.append("unknown_pmid")
    if used_evidence:
        for sent in re.split(r"(?<=[.!?])\s+", reply):
            if _STUDY_WORDS.search(sent) and unsupported_numbers(sent, trace_text, user_text):
                problems.append("unsupported_study_number")
                break
    if not used_evidence and _SPECULATE.search(reply):
        problems.append("speculation")
    if _CAUSAL.search(reply):
        problems.append("causal_claim")
    if data_context and unsupported_numbers(reply, trace_text, user_text):
        problems.append("unsupported_number")
    return list(dict.fromkeys(problems))


GROUNDING_NOTE = {
    "study_without_source": "You mentioned studies or research without looking anything up this turn. Remove it, or call find_evidence first.",
    "unknown_pmid": "You cited a PubMed id that was not in the search results. Cite only ids you were given.",
    "unsupported_study_number": "A number you attributed to research is not in the retrieved abstracts. Use only numbers from key_sentences.",
    "speculation": "You offered a guess about what might be behind the person's reading. Remove it. Describe the data, say you cannot tell the reason, "
                   "or call find_evidence and report what studies say, with the PubMed id.",
    "causal_claim": "You stated a cause. You cannot know the cause. Describe the numbers instead.",
    "unsupported_number": "You used a number that was not in the data provided. Use only numbers from the tool results or the alert, or leave it out.",
}
GROUNDING_FALLBACK = (
    "I want to be careful here. I can tell you what your numbers show, and I can look up what published studies say, "
    "but I cannot tell what is behind a change. Would you like me to look up the research on this?"
)
