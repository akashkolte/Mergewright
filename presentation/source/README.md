# Deck generator

The slides are native PowerPoint shapes built with PptxGenJS; build animations are injected afterwards.

```bash
npm install
node build.js deck_raw.pptx ../MergeWright_Presentation_Notes.md
python3 animate.py deck_raw.pptx ../MergeWright_Project_Proposal.pptx
```

`build.js` tags each animated shape's name as `A<click>_<effect>_<delayMs>_…` (click 0 plays on slide entry); `animate.py` turns those tags into PowerPoint timing XML and adds a fade transition to every slide. `build.js` also writes `theme.json`; the shipped deck had that palette written into its theme part so new shapes added in PowerPoint pick up the same colors.
