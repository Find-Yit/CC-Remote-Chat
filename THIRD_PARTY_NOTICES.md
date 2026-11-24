# Third-party notices

CC Remote Chat has no runtime dependencies (Python standard library + plain JavaScript, no npm, no CDN).
Parts of the frontend are adapted from **T3 Code** (https://github.com/pingdotgg/t3code):

| File | Adapted from |
|---|---|
| `static/lightbox.js` | zoom/pan algorithm of `ZoomableImage` |
| `static/filelinks.js` | file-path detection rules of `packages/shared/src/markdownLinks.ts` / `fileLinks.ts` |
| `static/toolgroups.js` | idea of `summarizeToolGroup` |
| `static/minimap.js` | port of `TimelineMinimap` (`apps/web/src/components/chat/TimelineMinimap.tsx`, `timelineMinimapItems.ts`, `MessagesTimeline.logic.ts`, `timelineMinimapWheel.ts`): structure, fisheye scale, preview card, keyboard, navigation buttons, wheel forwarding |
| `static/changes.js` | `formatCompactDiffCount` (`DiffStatLabel.tsx`), absolute → workspace-relative paths (`toolCallDiff.ts`), panel structure of `DiffPanel.tsx`, the Write "new file" rule of `ClaudeAdapterV2.ts` |
| `static/style.css` (parts) | layout ideas for tables, code blocks, hover metadata, tool groups |

T3 Code is licensed under the MIT License:

```
MIT License

Copyright (c) 2026 T3 Tools Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Parts of `static/changes.js` follow **Orca**
(https://github.com/stablyai/orca): merging several edits per turn and per file while keeping their order
(`src/shared/native-chat/tools/native-chat-turn-diffs.ts`) and treating a Write as a new file only when its result
starts with "File created successfully" (`native-chat-edit-normalize.ts`). Orca is licensed under the MIT License:

```
MIT License

Copyright (c) 2026 Lovecast Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The app icon and favicon (`assets/icon.svg`, `assets/AppIcon.icns`, `static/favicon.*`) use the Claude
logo, only to indicate that this tool is meant for Claude Code; the logo is not covered by this
project's MIT license.

"Claude" and "Claude Code" are trademarks of Anthropic, PBC. This project is not affiliated with,
endorsed by, or sponsored by Anthropic. It does not include or redistribute Claude Code; you install
and log in to Claude Code yourself.

## Components bundled in the dmg build (`tools/make_dmg.sh`)

The source tree has no third-party runtime code. The dmg build additionally ships these components inside
`CC Remote Chat.app/Contents/Resources/`; their full license texts are copied to `Contents/Resources/licenses/`
together with a `BUNDLED_COMPONENTS.txt` that records versions, URLs and SHA256 checksums:

| Component | Version | License |
|---|---|---|
| CPython (python-build-standalone build, trimmed) | 3.12.16 (20261110) | Python Software Foundation License; the build statically links OpenSSL (Apache-2.0), SQLite (public domain), libffi (MIT), xz/liblzma, bzip2, mpdecimal, expat, zstd and others (see https://github.com/astral-sh/python-build-standalone) |
| tmux | 3.6a | ISC License |
| libevent (statically linked into tmux) | 2.1.12-stable | BSD-3-Clause |
| utf8proc (statically linked into tmux) | 2.11.3 | MIT License (plus the Unicode data license) |

tmux links dynamically against the ncurses library that ships with macOS.
