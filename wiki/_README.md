# Wiki source files

These markdown files mirror the GitHub wiki at
<https://github.com/thuesdays/ghost_shell_browser/wiki>.

GitHub wikis are stored in a sibling git repository — for this repo
it's `git@github.com:thuesdays/ghost_shell_browser.wiki.git`. To
sync these files to the live wiki:

```powershell
cd F:\projects
git clone https://github.com/thuesdays/ghost_shell_browser.wiki.git
cd ghost_shell_browser.wiki

# Copy or rsync the source files in
copy ..\ghost_shell_browser\wiki\*.md .
del _README.md   # this index file isn't part of the wiki

git add -A
git commit -m "Wiki: sync from main repo for v0.2.0.11"
git push
```

The wiki must have at least one page already — if the wiki repo
404s on clone, go to the repo's wiki tab in GitHub UI once and
create a placeholder Home page. After that the `.wiki.git` URL
clones cleanly.

## Page index

- `Home.md` — landing page (sidebar on the wiki)
- `Quick-Start.md`
- `Architecture.md`
- `Profiles-and-Fingerprints.md`
- `Extensions.md`
- `Cookie-Pool.md`
- `Bulk-Create.md`
- `Flow-Steps-Reference.md`
- `Critical-Lessons.md`
- `Troubleshooting.md`
- `FAQ.md`

Filenames map directly to wiki page slugs — `Quick-Start.md`
becomes `https://github.com/thuesdays/ghost_shell_browser/wiki/Quick-Start`.
