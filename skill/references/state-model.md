# The five independent state dimensions

A row is described by five separate facts. Collapsing any two of them produces a
lie in the interface.

| Dimension | Values | Owned by |
| --- | --- | --- |
| Processing | `discovered`, `extracting`, `analyzing`, `ready`, `manual_review`, `error`, `stale` | the helper pipeline |
| Review decision | `unreviewed`, `keep`, `reject`, `hold` | the human reviewer |
| Actual location | `active`, `rejected`, `trash`, `missing`, `conflict` | verified filesystem reconciliation |
| Pending intent | `none`, `move_rejected`, `restore_active`, `move_trash`, `restore_previous` | a human request, or an unapproved agent proposal |
| Action execution | `planned`, `approved`, `applying`, `completed`, `partial`, `blocked`, `canceled` | the helper executor |

A row can legitimately read:

```text
decision = reject   location = active   intent = move_rejected   batch = applying
```

That is a file the reviewer rejected, which has not moved yet, with a saved
request to move it, in a batch that is running. The interface must show exactly
that. **It must not display a successful rejection-folder move before the move
actually happens.**

## Processing is independent of decision

A document can be `manual_review` (the parser could not read it) while the
reviewer's decision is `keep`. On a parser failure, any previous valid profile is
retained and visibly marked stale, and a manual-review task is created. Failures
never make rows disappear.

## What each decision implies for the filesystem

| User intent | Deterministic behaviour |
| --- | --- |
| Keep in the active folder | no filesystem move |
| Keep while in `Rejected/` | propose a return to the recorded active path; move only after approval |
| Reject | propose `Rejected/<document-id>/<original-filename>` |
| Hold or Unreviewed | preserve the current location; do not implicitly restore |
| Move to Trash | propose `Trash/<batch-id>/<document-id>/<filename>` |
| Restore from Trash | propose the previous recorded path, preserving the earlier decision |
| Missing source | block the action and create a reconciliation task |

A restored rejected file may legitimately return to `Rejected/`, because that was
its previous location. "Restore previous location" and "return to the active
folder" are different intents and are never collapsed into one Undo button.

## Terminology that the interface must get right

* **Submission** — one registered primary resume file. Multiple files from one
  person are not automatically merged into one candidate identity. Use
  "submissions" wherever a count could otherwise imply a verified number of
  distinct people.
* **Keep** — retain for further review, not hire.
* **Reject** — a human review disposition, not an email and not a file move.
* **Hold** — defer judgment.
* **Move to Trash** — a recoverable file-management action, not a hiring
  assessment.
* **`not_found`** — the processed document did not establish the criterion. It is
  **not** "unqualified", and it never sets a decision.

## Criterion results

| Result | Meaning |
| --- | --- |
| `supported` | relevant applicant-reported evidence was located |
| `not_found` | the processed document did not establish the criterion |
| `unclear` | relevant text exists but cannot safely establish the criterion |
| `needs_manual_review` | processing limits or quality prevent assessment |

No result directly sets Keep or Reject. `not_found` must never be rendered as
"does not have".

## Decision re-check

`decision_needs_recheck` is set when material input changes while a human decision
exists — a new source revision, or a changed criteria version. It flags the
decision for reconsideration. It never overwrites the decision automatically, and
it does not reopen a completed task.

## Unknown values

`null` means **unknown**, distinctly from `0` and from an empty string. A filter
must not silently hide unprocessed submissions; the default unknown policy is
`include_with_warning`, with a visible count and link for the rows it affected.
