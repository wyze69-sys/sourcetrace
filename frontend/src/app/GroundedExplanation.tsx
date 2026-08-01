interface GroundedExplanationProps {
  citedSteps: number[]
  text: string
  scopeNote: string
  summaryLabel: string
  sourceLabel: 'steps' | 'items'
}

interface ExplainBlock {
  kind: 'paragraph' | 'numbered' | 'bullet'
  /** Heading text for numbered items ("Initialization (Loading Data)") */
  title?: string
  /** Number label for numbered items ("1") */
  number?: string
  /** Bolded lead-in for bullet items ("Debounced Save") */
  lead?: string
  body: string
}

/** Matches "1. Title: body" or "1.  Title: body" numbered section starts. */
const NUMBERED_RE = /^(\d+)[.)]\s+(.+)$/
/** Matches bullet starts: "* ", "- ", or "• " (with optional bold lead "**Lead:**"). */
const BULLET_RE = /^[*\-•]\s+(.+)$/
/** Bold lead inside a bullet: "**Lead:** rest" */
const BOLD_LEAD_RE = /^\*\*(.+?)\*\*:?\s*(.*)$/
/** Citation tokens like [E3] / [S1] kept inline. */
const CITATION_RE = /\[([ES]\d+)\]/g

function splitTitleAndBody(text: string): { title?: string; body: string } {
  // "Title:" with nothing after — the line is a pure heading for what follows.
  if (text.endsWith(':') && text.length <= 72) {
    const title = text.slice(0, -1).replace(/\*\*/g, '').trim()
    if (title) return { title, body: '' }
  }
  // "Title: body" — only split on the first colon when it arrives early
  // (a real heading), not on colons deep inside prose.
  const colon = text.indexOf(':')
  if (colon > 0 && colon <= 64) {
    const title = text.slice(0, colon).replace(/\*\*/g, '').trim()
    const body = text.slice(colon + 1).trim()
    if (title && body) return { title, body }
  }
  return { body: text.replace(/\*\*/g, '') }
}

function parseExplanation(text: string): ExplainBlock[] {
  const rawLines = text
    .split(/\n/)
    .map((line) => line.trim())
    .filter(Boolean)

  const blocks: ExplainBlock[] = []
  let pendingParagraphs: string[] = []

  const flushParagraphs = () => {
    if (pendingParagraphs.length > 0) {
      blocks.push({ kind: 'paragraph', body: pendingParagraphs.join(' ') })
      pendingParagraphs = []
    }
  }

  for (const line of rawLines) {
    const numbered = NUMBERED_RE.exec(line)
    if (numbered) {
      flushParagraphs()
      const { title, body } = splitTitleAndBody(numbered[2])
      blocks.push({ kind: 'numbered', number: numbered[1], title, body })
      continue
    }

    const bullet = BULLET_RE.exec(line)
    if (bullet) {
      flushParagraphs()
      const boldLead = BOLD_LEAD_RE.exec(bullet[1])
      if (boldLead) {
        blocks.push({ kind: 'bullet', lead: boldLead[1], body: boldLead[2] })
      } else {
        blocks.push({ kind: 'bullet', body: bullet[1].replace(/\*\*/g, '') })
      }
      continue
    }

    // Continuation lines belong to the previous block when it exists,
    // otherwise accumulate as a plain paragraph.
    if (blocks.length > 0 && pendingParagraphs.length === 0) {
      const last = blocks[blocks.length - 1]
      last.body = last.body ? `${last.body} ${line.replace(/\*\*/g, '')}` : line
    } else {
      pendingParagraphs.push(line.replace(/\*\*/g, ''))
    }
  }
  flushParagraphs()

  return blocks
}

/** Render body text with citation chips ([E3], [S1]) highlighted inline. */
function BodyText({ text }: { text: string }) {
  const parts = text.split(CITATION_RE)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <span key={i} className="grounded-citation-ref">
            [{part}]
          </span>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  )
}

export function GroundedExplanation({
  citedSteps,
  text,
  scopeNote,
  summaryLabel,
  sourceLabel,
}: GroundedExplanationProps) {
  const blocks = parseExplanation(text)
  const hasStructure = blocks.some((b) => b.kind !== 'paragraph')
  const citedText = citedSteps.map((step) => `S${step}`).join(', ')

  // When the text has no numbered/bulleted structure, treat the whole text
  // as the summary paragraph (existing behaviour).
  const summary = hasStructure ? null : blocks[0]?.body ?? text

  return (
    <section className="grounded-explanation" aria-label="Grounded explanation">
      <div className="grounded-explanation-header">
        <div>
          <p className="grounded-kicker">Evidence-backed explanation</p>
          <h3>{summaryLabel}</h3>
        </div>
        <span className="grounded-evidence-count">
          {citedSteps.length} {citedSteps.length === 1 ? 'source' : 'sources'}
        </span>
      </div>

      {summary && (
        <div className="grounded-summary">
          <p className="grounded-section-label">In plain language</p>
          <p className="grounded-summary-text">{summary}</p>
        </div>
      )}

      {hasStructure && (
        <div className="grounded-details">
          <p className="grounded-section-label">How it works</p>
          <ol className="grounded-step-list">
            {blocks.map((block, index) => {
              if (block.kind === 'numbered') {
                return (
                  <li key={`${block.number}-${index}`} className="grounded-step">
                    <span className="grounded-step-number">{block.number}</span>
                    <div className="grounded-step-content">
                      {block.title && <strong className="grounded-step-title">{block.title}</strong>}
                      <p className="grounded-step-body">
                        <BodyText text={block.body} />
                      </p>
                    </div>
                  </li>
                )
              }
              if (block.kind === 'bullet') {
                return (
                  <li key={`b-${index}`} className="grounded-substep">
                    {block.lead && <strong className="grounded-substep-lead">{block.lead}:</strong>}{' '}
                    <BodyText text={block.body} />
                  </li>
                )
              }
              return (
                <li key={`p-${index}`} className="grounded-plain">
                  <p className="grounded-step-body">
                    <BodyText text={block.body} />
                  </p>
                </li>
              )
            })}
          </ol>
        </div>
      )}

      <div className="grounded-source-row">
        <span className="grounded-section-label">Sources used</span>
        <span className="grounded-source-context">cites {sourceLabel} {citedText}</span>
        <div className="grounded-source-list">
          {citedSteps.map((step) => (
            <span key={step} className="grounded-source-chip">
              S{step}
            </span>
          ))}
        </div>
      </div>

      <p className="grounded-scope-note">{scopeNote}</p>
    </section>
  )
}

export default GroundedExplanation
