# Answering "can I read this on a Kindle?"

This was the least-specified capability in the build brief, which flagged it as
needing a spike before committing to an output shape. Here is what that spike
found and what `check_ebook_formats` actually does.

## BiblioCommons cannot answer it

The brief's hypothesis was that Kindle status might be in the record page's
embedded JSON, and if not, reachable by following `availability.eresourceUrl`
into the OverDrive detail page. Neither holds:

- **No per-format data in the page state.** A digital bib's `availability` and
  `policy` say it is an `EBOOK` from `OverDriveAPI` and whether it is holdable.
  There is nothing about EPUB vs PDF vs Kindle.
- **`eresourceUrl` is `null`** on every digital bib inspected, on search pages
  and record pages alike. There is no link to follow.
- The only occurrences of "Kindle" in a record page's state belong to the UI's
  **localisation dictionary** (`digiformat_kindle: "Kindle"`) — a label the
  interface *could* render, not a fact about the title — and to a static nav
  link into OverDrive's site-wide Kindle-filtered search.

Concluding "no Kindle" from BiblioCommons alone would be a guess.

## Where the answer actually lives

OverDrive's public web API, the unauthenticated endpoint the library's own
OverDrive site calls from a browser:

```
GET https://thunder.api.overdrive.com/v2/libraries/{library}/media?query=…
GET https://thunder.api.overdrive.com/v2/libraries/{library}/media/{id}
```

Each title carries a `formats[]` array, and `ebook-kindle` in it is exactly the
"Read now with Kindle" affordance. The Kindle format also carries an **ASIN**,
which is a concrete, checkable artefact rather than a boolean we inferred.

This needs **no authentication**, so `check_ebook_formats` — which the brief
assumed would require login — is a public call.

> **A judgment call worth naming.** The brief said OverDrive *partner API*
> access was not granted, and to stick to what the authenticated web surfaces
> expose. Thunder is not the partner API: it is the public web backend of the
> library's own OverDrive site, in the same category as the BiblioCommons pages
> this server already parses. Using it is a deliberate choice, and it is
> reversible — set `use_overdrive_thunder = false` and the tool reports Kindle
> status as `null` with an explanation instead of guessing.

## Joining the two catalogs

BiblioCommons bib ids (`S30C2949104`, an id from before Seattle's 2026
renumbering; current ids look like `S246C…`, `library.id` 246) and OverDrive
ids (`1243691`) are unrelated, so the records must be joined. The ladder, in
order:

1. **ISBN.** `briefInfo.isbns` on the BiblioCommons record contains the ISBN
   OverDrive records against its EPUB formats. A Thunder search for it returns
   the single matching title. This is exact and resolves the common case.
2. **Title + author**, if no ISBN matches.
3. **Media type**, to separate the ebook from the audiobook of the same title by
   the same author — the most likely wrong answer. Thunder ignores the
   `mediaType` query parameter, so this filters client-side on each item's
   `type.id`.
4. **Copy counts.** Both catalogs report the same holdings for the same record,
   so an exact `ownedCopies` / `totalCopies` agreement identifies the edition.

If more than one candidate survives all four, the tool **reports ambiguity**
rather than picking one.

## Guarding against a wrong join

A bad join is the failure mode that would pass silently — plausible formats
reported for the wrong edition. Two defences:

- **Corroboration is returned, not just consumed.** Every report includes both
  catalogs' copy and hold counts side by side. For the Dune ebook, OverDrive
  says 15 owned / 74 holds and BiblioCommons says 15 total / 74 held. A
  mismatch is visible in the tool output.
- **`matched_by` records how the join was made** (`isbn:9781101658055`,
  `title+author+ebook`, …), so the agent can weigh the answer's confidence.

## Output

`kindle_available` is deliberately three-valued:

| value | meaning |
|---|---|
| `true` | OverDrive lists `ebook-kindle`; `kindle_asin` is populated |
| `false` | The title was found and Kindle is genuinely not among its formats |
| `null` | Could not be determined — with a `note` saying why |

The distinction between `false` and `null` is the point. "We couldn't find out"
must never be reported as "no".

## Non-ebook editions

The tool accepts any edition's bib id. Given a physical or audiobook edition it
resolves the **ebook edition of the same work** — matched on `groupKey`, so
unrelated records on the page can't be picked up — and says so in `note`. If the
work has no ebook edition at all, it reports that plainly.
