from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HurricaneStormClassification:
    research_only: bool
    tradable: bool
    source: str | None
    reasons: list[str]


def classify_hurricane_storm_market(
    title: str,
    description: str | None = None,
) -> HurricaneStormClassification:
    text = f"{title}\n{description or ''}"
    if not re.search(
        r"\b(hurricane|tropical storm|storm|cyclone|typhoon|landfall|NHC)\b", text, re.I
    ):
        return HurricaneStormClassification(
            research_only=False,
            tradable=False,
            source=None,
            reasons=["not a hurricane/storm market"],
        )
    source = "NHC" if re.search(r"\bNHC\b|National Hurricane Center", text, re.I) else None
    if source is None and re.search(r"\bNOAA\b", text, re.I):
        source = "NOAA"
    reasons = ["storm pricing requires dedicated NHC adapter"]
    if source is None:
        reasons.append("unclear NOAA/NHC source")
    return HurricaneStormClassification(
        research_only=True,
        tradable=False,
        source=source,
        reasons=reasons,
    )
