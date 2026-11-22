Return boolean from Markdown.goto_anchor
Hey again!

I'm updating my code to work with Textual now that it has fixed going to anchors in a Markdown document.
Textual is working well, however I'm facing an issue while trying to extend its behavior.

Basically, in the Markdown documents I generate (in my API docs textual browser :wink:), I can have both anchors that exist within the document, and anchors that do not. When the anchor exist, I want to simply go to that anchor. When it doesn't exist, I want to generate the corresponding Markdown and update the viewer with it.

The issue is that there's no easy way to know if the anchor exists. I'd have to either:

- check somehow if the document scrolled to somewhere else after the anchor was clicked (no idea if that is possible, doesn't sound robust/efficient)
- rebuild the list of slugs myself, accessing private variables like `Markdown._table_of_contents`, and slugify my anchor to check if it's within this list

Instead, it would be super convenient if the `Markdown.goto_anchor` method returned a boolean: true when it found the anchor, false otherwise. That way I can simply do this in my own code:

```python
class GriffeMarkdownViewer(MarkdownViewer):
    async def _on_markdown_link_clicked(self, message: Markdown.LinkClicked) -> None:
        message.prevent_default()
        anchor = message.href
        if anchor.startswith("#"):
            # Try going to the anchor in the current document.
            if not self.document.goto_anchor(anchor.lstrip("#").replace(".", "").lower()):
                try:
                    # Anchor not on the page: it's another object, load it and render it.
                    markdown = _to_markdown(self.griffe_loader, anchor.lstrip("#"))
                except Exception as error:  # noqa: BLE001,S110
                    logger.exception(error)
                else:
                    self.document.update(markdown)
        else:
            # Try default behavior of the viewer.
            await self.go(anchor)
```

Emphasis on `if not self.document.goto_anchor(...)` :slightly_smiling_face: 

The change is easy and backward-compatible:

```python
    def goto_anchor(self, anchor: str) -> bool:
        if not self._table_of_contents or not isinstance(self.parent, Widget):
            return False
        unique = TrackedSlugs()
        for _, title, header_id in self._table_of_contents:
            if unique.slug(title) == anchor:
                self.parent.scroll_to_widget(self.query_one(f"#{header_id}"), top=True)
                return True
        return False
```

If you think that is worth adding, I can send a PR!

We can also discuss further, as there are probably other ways to make this possible/easier, for example by adding a `Markdown.anchor_exists` method :shrug: 