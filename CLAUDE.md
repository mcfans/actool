# Project Instructions

<!-- deciduous:start -->
## Decision Graph Workflow

**THIS IS MANDATORY. Log decisions IN REAL-TIME, not retroactively.**

### Available Slash Commands

| Command | Purpose |
|---------|---------|
| `/decision` | Manage decision graph - add nodes, link edges, sync |
| `/recover` | Recover context from decision graph on session start |
| `/work` | Start a work transaction - creates goal node before implementation |
| `/document` | Generate comprehensive documentation for a file or directory |
| `/build-test` | Build the project and run the test suite |
| `/serve-ui` | Start the decision graph web viewer |
| `/sync-graph` | Export decision graph to GitHub Pages |
| `/decision-graph` | Build a decision graph from commit history |
| `/sync` | Multi-user sync - pull events, rebuild, push |

### Available Skills

| Skill | Purpose |
|-------|---------|
| `/pulse` | Map current design as decisions (Now mode) |
| `/narratives` | Understand how the system evolved (History mode) |
| `/archaeology` | Transform narratives into queryable graph |

### The Node Flow Rule - CRITICAL

The canonical flow through the decision graph is:

```
goal -> options -> decision -> actions -> outcomes
```

- **Goals** lead to **options** (possible approaches to explore)
- **Options** lead to a **decision** (choosing which option to pursue)
- **Decisions** lead to **actions** (implementing the chosen approach)
- **Actions** lead to **outcomes** (results of the implementation)
- **Observations** attach anywhere relevant
- Goals do NOT lead directly to decisions -- there must be options first
- Options do NOT come after decisions -- options come BEFORE decisions
- Decision nodes should only be created when an option is actually chosen, not prematurely

### The Core Rule

```
BEFORE you do something -> Log what you're ABOUT to do
AFTER it succeeds/fails -> Log the outcome
CONNECT immediately -> Link every node to its parent
AUDIT regularly -> Check for missing connections
```

### Behavioral Triggers - MUST LOG WHEN:

| Trigger | Log Type | Example |
|---------|----------|---------|
| User asks for a new feature | `goal` **with -p** | "Add dark mode" |
| Exploring possible approaches | `option` | "Use Redux for state" |
| Choosing between approaches | `decision` | "Choose state management" |
| About to write/edit code | `action` | "Implementing Redux store" |
| Something worked or failed | `outcome` | "Redux integration successful" |
| Notice something interesting | `observation` | "Existing code uses hooks" |

### Document Attachments

Attach files (images, PDFs, diagrams, specs, screenshots) to decision graph nodes for rich context.

```bash
# Attach a file to a node
deciduous doc attach <node_id> <file_path>
deciduous doc attach <node_id> <file_path> -d "Architecture diagram"
deciduous doc attach <node_id> <file_path> --ai-describe

# List documents
deciduous doc list              # All documents
deciduous doc list <node_id>    # Documents for a specific node

# Manage documents
deciduous doc show <doc_id>     # Show document details
deciduous doc describe <doc_id> "Updated description"
deciduous doc describe <doc_id> --ai   # AI-generate description
deciduous doc open <doc_id>     # Open in default application
deciduous doc detach <doc_id>   # Soft-delete (recoverable)
deciduous doc gc                # Remove orphaned files from disk
```

**When to suggest document attachment:**

| Situation | Action |
|-----------|--------|
| User shares an image or screenshot | Ask: "Want me to attach this to the current goal/action node?" |
| User references an external document | Ask: "Should I attach a copy to the decision graph?" |
| Architecture diagram is discussed | Suggest attaching it to the relevant goal node |
| Files not in the project are dropped in | Attach to the most relevant active node |

**Do NOT aggressively prompt for documents.** Only suggest when files are directly relevant to a decision node. Files are stored in `.deciduous/documents/` with content-hash naming for deduplication.

### CRITICAL: Capture VERBATIM User Prompts

**Prompts must be the EXACT user message, not a summary.** When a user request triggers new work, capture their full message word-for-word.

**BAD - summaries are useless for context recovery:**
```bash
# DON'T DO THIS - this is a summary, not a prompt
deciduous add goal "Add auth" -p "User asked: add login to the app"
```

**GOOD - verbatim prompts enable full context recovery:**
```bash
# Use --prompt-stdin for multi-line prompts
deciduous add goal "Add auth" -c 90 --prompt-stdin << 'EOF'
I need to add user authentication to the app. Users should be able to sign up
with email/password, and we need OAuth support for Google and GitHub. The auth
should use JWT tokens with refresh token rotation.
EOF

# Or use the prompt command to update existing nodes
deciduous prompt 42 << 'EOF'
The full verbatim user message goes here...
EOF
```

**When to capture prompts:**
- Root `goal` nodes: YES - the FULL original request
- Major direction changes: YES - when user redirects the work
- Routine downstream nodes: NO - they inherit context via edges

**Updating prompts on existing nodes:**
```bash
deciduous prompt <node_id> "full verbatim prompt here"
cat prompt.txt | deciduous prompt <node_id>  # Multi-line from stdin
```

Prompts are viewable in the web viewer.

### CRITICAL: Maintain Connections

**The graph's value is in its CONNECTIONS, not just nodes.**

| When you create... | IMMEDIATELY link to... |
|-------------------|------------------------|
| `outcome` | The action that produced it |
| `action` | The decision that spawned it |
| `decision` | The option(s) it chose between |
| `option` | Its parent goal |
| `observation` | Related goal/action |
| `revisit` | The decision/outcome being reconsidered |

**Root `goal` nodes are the ONLY valid orphans.**

### Quick Commands

```bash
deciduous add goal "Title" -c 90 -p "User's original request"
deciduous add action "Title" -c 85
deciduous link FROM TO -r "reason"  # DO THIS IMMEDIATELY!
deciduous serve   # View live (auto-refreshes every 30s)
deciduous sync    # Export for static hosting

# Metadata flags
# -c, --confidence 0-100   Confidence level
# -p, --prompt "..."       Store the user prompt (use when semantically meaningful)
# -f, --files "a.rs,b.rs"  Associate files
# -b, --branch <name>      Git branch (auto-detected)
# --commit <hash|HEAD>     Link to git commit (use HEAD for current commit)
# --date "YYYY-MM-DD"      Backdate node (for archaeology)

# Branch filtering
deciduous nodes --branch main
deciduous nodes -b feature-auth
```

### CRITICAL: Link Commits to Actions/Outcomes

**After every git commit, link it to the decision graph!**

```bash
git commit -m "feat: add auth"
deciduous add action "Implemented auth" -c 90 --commit HEAD
deciduous link <goal_id> <action_id> -r "Implementation"
```

The `--commit HEAD` flag captures the commit hash and links it to the node. The web viewer will show commit messages, authors, and dates.

### Git History & Deployment

```bash
# Export graph AND git history for web viewer
deciduous sync

# This creates:
# - docs/graph-data.json (decision graph)
# - docs/git-history.json (commit info for linked nodes)
```

To deploy to GitHub Pages:
1. `deciduous sync` to export
2. Push to GitHub
3. Settings > Pages > Deploy from branch > /docs folder

Your graph will be live at `https://<user>.github.io/<repo>/`

### Branch-Based Grouping

Nodes are auto-tagged with the current git branch. Configure in `.deciduous/config.toml`:
```toml
[branch]
main_branches = ["main", "master"]
auto_detect = true
```

### Audit Checklist (Before Every Sync)

1. Does every **outcome** link back to what caused it?
2. Does every **action** link to why you did it?
3. Any **dangling outcomes** without parents?

### Git Staging Rules - CRITICAL

**NEVER use broad git add commands that stage everything:**
- ❌ `git add -A` - stages ALL changes including untracked files
- ❌ `git add .` - stages everything in current directory
- ❌ `git add -a` or `git commit -am` - auto-stages all tracked changes
- ❌ `git add *` - glob patterns can catch unintended files

**ALWAYS stage files explicitly by name:**
- ✅ `git add src/main.rs src/lib.rs`
- ✅ `git add Cargo.toml Cargo.lock`
- ✅ `git add .claude/commands/decision.md`

**Why this matters:**
- Prevents accidentally committing sensitive files (.env, credentials)
- Prevents committing large binaries or build artifacts
- Forces you to review exactly what you're committing
- Catches unintended changes before they enter git history

### Session Start Checklist

```bash
deciduous check-update    # Update needed? Run 'deciduous update' if yes
                          # (auto-checked every 24h if auto-update is on)
deciduous nodes           # What decisions exist?
deciduous edges           # How are they connected? Any gaps?
deciduous doc list        # Any attached documents to review?
git status                # Current state
```

### Multi-User Sync

Sync decisions with teammates via event logs:

```bash
# Check sync status
deciduous events status

# Apply teammate events (after git pull)
deciduous events rebuild

# Compact old events periodically
deciduous events checkpoint --clear-events
```

Events auto-emit on add/link/status commands. Git merges event files automatically.
<!-- deciduous:end -->
The project's purpose is to provide a clean reimplementation of macos's actool. The initial main goal is to provide the basic compilation step. It should be able to run:
```
actool --compile "test_outdir" --platform macosx --minimum-deployment-target "11.0" --app-icon AppIcon --output-partial-info-plist "test_outdir/AppIcon.Info.plist" test/Images.xcassets
```
and create an identical archive compared to the system version.

You can use /usr/bin/actool which will call the system version. Don't try to use it to read/write in /tmp and other places which may be shared - use local subdirectories.

You can create tools to help the analysis, comparison, compiling, etc. but the main app should have the cli and arguments compatible with the original.

Use Rust integration tests under `tests/` (one file per area, e.g. `packing_regressions.rs`, `dmp2_regressions.rs`) for full-flow coverage, and run `tools/validate_repos.py --only <slug>` to check results against third-party repositories.
Don't use comments that repeat the code logic. Only use comments for details about why the code is there.

## Debugging `.car` parity issues

Three complementary tools — use all three:
- `./tools/validate_car <path>` — runs CoreUI's actual `imagesWithName:` / `colorWithName:` paths. Closest signal to "does it work in a real app?" Apple's reference reports 9 OK / 1 FAIL on the element-web fixture; that's the parity target.
- `python3 tools/compare_car.py <a> <b>` — structural diff; now audits BOM physical layout (inline-key region, BITMAPKEYS, tree headers, named-block order) — the categories most likely to cause silent CUICatalog failures.
- `/usr/bin/actool` is the reference. Compile our output and Apple's into separate local dirs (not `/tmp/`-shared) at the same `--minimum-deployment-target` and diff.

Element-web `.icon` at `third_party/element-web/apps/desktop/build/icon.icon/` is the canonical IconComposer fixture (1 group, 1 layer, `fill: "automatic"` → 5 Color + 2 Gradient renditions).

### `.icon` (IconComposer) catalog gotchas that cost many sessions to find

These are all *silent* — the catalog loads, but `imagesWithName:` returns empty. None surface in a naive tree-walk diff.

- **BOM leaf inline-key region.** Fixed-key trees (RENDITIONS, APPEARANCEKEYS) pack the rendition keys directly after the entry table, separated by a 4-byte zero gap, then pad to total leaf size = `block_size + n × key_size`. CUICatalog reads facet→rendition mappings from this packed region. If it's zero-filled or wrong-offset, every lookup silently returns empty even when the separate key blocks are byte-perfect. See `bom.rs::build_leaf_node`.
- **CARHEADER coreUI version must be 975 for IconComposer.** Lower values activate the legacy path and lookups fail. `car::make_carheader_versioned(n, 975)` for `.icon`; default `972` elsewhere.
- **BITMAPKEYS must list every facet identifier** with a 52-byte attribute-mask blob (`-1` for variable attrs 7/1/2/17; bitmask of seen values for the rest). See `icon_bundle::build_bitmapkeys`. An empty tree breaks every facet lookup.
- **Color/Gradient rendition KEYS use `scale=1`** even though the CSI's `scale_factor` field is 0. `colorWithName:displayGamut:` filters at the key level.
- **Pre-rendered icon PNG renditions need `template_rendering_intent: 0`** (original). The default `-1` → automatic (4) sets a flag that makes CUICatalog look for a template variant.
- **`.icon` layer assets need `force_non_opaque: true`** so CELM encodes ver=0 (non-opaque) regardless of source alpha — layers always composite with alpha inside iconstack rendering.

Other surprising behaviors:
- Apple emits the **same** `.car` at every `--minimum-deployment-target` from 11.0–26.0 — only the `deploymentPlatformVersion` string in EXTENDED_METADATA changes. No version-specific output paths.
- For `.icon` bundles Apple **never** writes `.icns` and **always** writes an empty `<dict/>` plist (181 bytes), regardless of `--standalone-icon-behavior`. The legacy xcassets path (`compiler.rs`) still emits both.
- BOM named-block emission order: `CARHEADER, RENDITIONS, FACETKEYS, APPEARANCEKEYS, KEYFORMAT, EXTENDED_METADATA, BITMAPKEYS` (RENDITIONS early, KEYFORMAT late) — not load-blocking on its own but reduces the diff.
