# Live Gloo verification

Run on October 1, 2026, using synthetic sample texts and no SMS delivery.

Both pinned models passed a live Responses API preflight:

- Parser: `gloo-openai-gpt-5-mini`
- Agent: `gloo-anthropic-claude-sonnet-4.6`

The live text-signup check separately returned `signup_pending` and created a
coordinator approval without sending SMS.

The 43-text sample set reached 36/43 intent matches and 42/43 sensitive-flag
matches. Seven intent mismatches remain for further prompt/core evaluation.
The one sensitivity mismatch was a conservative false positive on a volunteer
stepping back from nursery; it did not miss a sensitive example. No eval cases
or pass criteria were changed.

```text
text                                             expected      got           conf  sens  ok
-------------------------------------------------------------------------------------------
cant make it tmrw sorry!!                        cancel        cancel        0.98  False OK
which sunday?                                    question      question      0.90  False OK
i can but only til 10:30                         partial       partial       0.95  False OK
Y                                                accept        accept        0.95  False OK
yes!!                                            accept        accept        0.95  False OK
who is this                                      question      question      0.98  False OK
not this month                                   availability  availability  0.95  False OK
2nd and 4th                                      availability  availability  0.96  False OK
same as usual                                    availability  availability  0.95  False OK
STOP                                             other         other         0.98  False OK
my dad was just taken to the ER, can't come      cancel        cancel        0.98  True  OK
I'm not doing well and don't want to be aro...   cancel        cancel        0.80  True  OK
I finished the safety training last week so...   other         availability  0.95  False MISS
sorry can't do sunday                            cancel        cancel        0.95  False OK
yes I can help                                   accept        accept        0.95  False OK
no sorry                                         decline       decline       0.95  False OK
maybe? have to check with my wife                unclear       unclear       0.88  False OK
What time does it start?                         question      question      0.96  False OK
I'll be there                                    confirm       confirm       0.98  False OK
C                                                confirm       confirm       0.95  False OK
X                                                cancel        cancel        0.90  False OK
can i switch to the 11?                          other         question      0.90  False MISS
we're out of town oct 18                         availability  availability  0.95  False OK
sure thing 👍                                     accept        accept        0.98  False OK
START                                            other         unclear       0.45  False MISS
who's preaching this week                        question      question      0.95  False OK
My mom passed away last night. I can't come...   cancel        cancel        0.98  True  OK
in the hospital with my son, prayers apprec...   other         other         0.90  True  OK
I can do first and third sundays                 availability  availability  0.98  False OK
all sundays work except the 11th                 availability  availability  0.95  False OK
only mornings                                    availability  availability  0.95  False OK
cant do nursery anymore, too much for me ri...   other         cancel        0.95  True  MISS
yes but I have to leave by 10:30                 partial       partial       0.95  False OK
is this the church?                              question      question      0.95  False OK
wrong number                                     other         other         0.95  False OK
thanks!                                          other         other         0.98  False OK
🙏                                                unclear       unclear       0.95  False OK
ok                                               unclear       confirm       0.90  False MISS
I never signed up for this                       other         decline       0.90  False MISS
can Jen cover for me?                            cancel        cancel        0.95  False OK
running 15 min late today                        other         other         0.95  False OK
surgery next week so I'm out for the month       cancel        cancel        0.98  True  OK
actually I can make it after all                 other         accept        0.96  False MISS

intent: 36/43 (84%)   sensitive flag: 42/43 (98%)
model: gloo-openai-gpt-5-mini   tokens: 83361 in / 12286 out over 45 calls
```

## Replacement selection and text signup, October 1, 2026

The fill prompt now lets Gloo select from the full eligible pool. The first
live run selected three people but used a descriptive string for the outreach
purpose, so the send gate rejected its asks. Prompt v3 and an outreach-only
schema enum fixed this. A completed-outreach postcondition prevents a false
success when selected volunteers were not actually asked.

The follow-up live run selected three eligible people, sent three mock asks,
interpreted a YES through the live Gloo parser and confirmed the replacement.
A second conversation completed JOIN → Alex Morgan → YES, creating and
activating an unqualified volunteer profile with explicit text consent and
no admin approval. Checks used an in-memory database and mock SMS throughout.
