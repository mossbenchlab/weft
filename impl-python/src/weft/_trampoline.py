"""Generator trampoline — runs mutually recursive generator functions on
an explicit heap stack, so tree depth never consumes host call-stack
frames and ``sys.setrecursionlimit`` is never touched.

Convention: a *step generator* performs its work and, wherever it would
have made a recursive call, instead ``yield``s the child step generator;
the driver runs the child and ``send``s the child's return value
(``StopIteration.value``) back in. A step generator returns its result
with a plain ``return``.

    def walk(node):            # a step generator
        total = 1
        for child in node.children:
            total += (yield walk(child))
        return total

    result = drive(walk(root))
"""
from __future__ import annotations

from typing import Any, Generator

Step = Generator[Any, Any, Any]


def drive(root: Step) -> Any:
    """Run ``root`` (and every step generator it yields, transitively) to
    completion on an explicit stack; returns the root's return value.
    Exceptions raised inside any step propagate to the caller; suspended
    generators on the stack are closed first so their ``finally`` blocks
    run in the expected inner-to-outer order."""
    stack: list = [root]
    send_val: Any = None
    try:
        while True:
            try:
                child = stack[-1].send(send_val)
            except StopIteration as done:
                stack.pop()
                if not stack:
                    return done.value
                send_val = done.value
                continue
            stack.append(child)
            send_val = None
    except BaseException:
        while stack:
            stack.pop().close()
        raise
