interface GroundedExplanationProps {
  citedSteps: number[]
  text: string
  scopeNote: string
  summaryLabel: string
  sourceLabel: 'steps' | 'items'
}

function splitExplanation(text: string): string[] {
  return text
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
}

export function GroundedExplanation({
  citedSteps,
  text,
  scopeNote,
  summaryLabel,
  sourceLabel,
}: GroundedExplanationProps) {
  const paragraphs = splitExplanation(text)
  const summary = paragraphs[0] || text
  const details = paragraphs.slice(1)
  const citedText = citedSteps.map((step) => `S${step}`).join(', ')

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

      <div className="grounded-summary">
        <p className="grounded-section-label">In plain language</p>
        <p className="grounded-summary-text">{summary}</p>
      </div>

      {details.length > 0 && (
        <div className="grounded-details">
          <p className="grounded-section-label">How the evidence supports this</p>
          {details.map((paragraph, index) => (
            <p key={`${paragraph.slice(0, 24)}-${index}`}>{paragraph}</p>
          ))}
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
