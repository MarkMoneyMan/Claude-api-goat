"""
Dedicated "known clean" fixture for self-check.yml's second job.

Why this file exists (and why rules.py stopped working as this fixture):
self-check.yml's "expect-clean-on-own-source" job needs a target that is
*guaranteed* to produce zero findings, forever, as a way of asserting the
action doesn't false-positive on ordinary code. rules.py looked like a
reasonable choice at first ("it doesn't call the Anthropic API itself"),
but that reasoning was wrong: rules.py's entire job is to store the exact
trigger strings the rules look for (things like "client.beta.files",
"managed-agents-2026-04-01", "compaction_control") as pattern/title/detail
text inside its own NEW_RULES assignment. The generic engine's ast.Assign
candidate type matches that assignment, so rules.py self-matches several
of its own rules — confirmed by a real GitHub Actions run going red
(run #1, commit fedb0e7) and reproduced locally with
`python3 ast_scan.py rules.py` (7 findings, several HIGH).

"Doesn't call the Anthropic API" and "contains no matching text" are not
the same property. This file is deliberately unrelated to the Anthropic
SDK in both senses: it doesn't call the API, and it contains none of the
literal strings any rule in rules.py looks for. That's the actual
invariant the job needs.
"""

from dataclasses import dataclass


@dataclass
class Point:
    x: float
    y: float

    def distance_to(self, other: "Point") -> float:
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


def midpoint(a: Point, b: Point) -> Point:
    return Point((a.x + b.x) / 2, (a.y + b.y) / 2)


if __name__ == "__main__":
    p1 = Point(0, 0)
    p2 = Point(3, 4)
    print(f"distance: {p1.distance_to(p2)}")
    print(f"midpoint: {midpoint(p1, p2)}")
