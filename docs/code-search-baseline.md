# Code search: local baseline for a JevGrep comparison

This is a three-question development sample, not a held-out accuracy benchmark. The corpus is the public `hermes-jev-skills` tree at `6e606eb66389b6556d694bb1069bc5a9c8288f7b`. Questions omit function names. The local baseline uses hand-picked keywords and verifies excerpts in the pinned tree. No third-party code was installed or executed, and no repository text was sent to a model.

| Question | Grep pattern | Verified evidence |
| --- | --- | --- |
| Where does the example compute readable foreground/background contrast? | `contrast\|luminance` | `jevkit/design_choice.py:46–56` |
| Where is the decision API's remaining time recalculated across retries? | `wall.clock budget\|deadline` | `jevkit/client.py:692–712` (the first hit is the nearby docstring; read the implementation) |
| What prevents an old model choice from returning when a turn's connection changes and then changes back? | `context changed\|context_changed\|invalidated` | `hermes/plugin/hermes-jev/__init__.py:469–490` |

Reproduce from a clone of this repo. Use the patterns above with `|` as the regex alternation operator:

```sh
rev=6e606eb66389b6556d694bb1069bc5a9c8288f7b
time git grep -inE 'contrast|luminance' "$rev" -- jevkit hermes/plugin
git show "$rev:jevkit/design_choice.py" | nl -ba | sed -n '46,56p'
```

The three local searches led to all three verified excerpts. On one run, the grep subprocesses alone took about 16–19 ms each; this excludes choosing patterns and reading evidence. Model input, API calls, and external source disclosure were zero for this baseline. These cases are useful for exercising a harness, not for concluding that one search method is better.

For a paired trial, pin [JevGrep](https://github.com/nassim-arifette/jevgrep) and the corpus, freeze a fresh set of questions plus expected source ranges before running either method, and give both methods the same time and output limits. Score whether the final excerpts contain the supporting lines, not whether a result mentions the right file. Record end-to-end time (including the local query/read steps), source bytes sent to the provider, request count, and estimated cost separately. Only use a public corpus explicitly authorized for provider submission, and set a spending cap before any live trial. A scripted provider can check plumbing but cannot measure semantic retrieval.
