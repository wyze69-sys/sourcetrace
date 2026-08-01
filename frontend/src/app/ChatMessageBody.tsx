/**
 * Lightweight, dependency-free renderer for chat message text.
 *
 * Handles the markdown-ish output the backend returns:
 *   1.  **Bold titles:** "1. Initialization (Loading Data):"
 *   2.  Numbered items   "1. Some item"
 *   3.  Bullets          "* item" / "- item" / "• item" with "**Lead:**" support
 *   4.  Inline bold      "**some text**"
 *
 * Without this, raw `**` and list markers render as literal text, which
 * looks noisy and unpolished in the chat bubble.
 */

interface ChatBlock {
  kind: 'number' | 'bullet' | 'paragraph'
  number?: string
  lead?: string
  text: string
}

const NUMBERED = /^(\d+)[.)]\s+(.+)$/
const BULLET = /^[*•-]\s+(.+)$/
const BOLD_LEAD = /^\*\*(.+?)\*\*:?\s*(.*)$/
const INLINE_BOLD = /\*\*([^*]+)\*\*/g

function parseBlocks(text: string): ChatBlock[] {
  const lines = text.split(/\n/).map((l) => l.trim()).filter(Boolean)
  const blocks: ChatBlock[] = []

  for (const line of lines) {
    const numbered = NUMBERED.exec(line)
    if (numbered) {
      // Split a trailing-colon heading from its body ("2. Saving Data:" vs "2. Intro: details")
      const rest = numbered[2]
      const leadMatch = BOLD_LEAD.exec(rest) || null
      if (leadMatch) {
        blocks.push({ kind: 'number', number: numbered[1], lead: leadMatch[1], text: leadMatch[2] })
      } else {
        const colon = rest.indexOf(':')
        if (colon > 0 && colon <= 64) {
          const title = rest.slice(0, colon).trim()
          const body = rest.slice(colon + 1).trim()
          blocks.push({ kind: 'number', number: numbered[1], lead: title, text: body })
        } else {
          blocks.push({ kind: 'number', number: numbered[1], text: rest })
        }
      }
      continue
    }

    const bullet = BULLET.exec(line)
    if (bullet) {
      const leadMatch = BOLD_LEAD.exec(bullet[1])
      if (leadMatch) {
        blocks.push({ kind: 'bullet', lead: leadMatch[1], text: leadMatch[2] })
      } else {
        blocks.push({ kind: 'bullet', text: bullet[1] })
      }
      continue
    }

    // Continuation of the previous block when no marker, else a paragraph
    const prev = blocks[blocks.length - 1]
    if (prev && (prev.kind === 'number' || prev.kind === 'bullet')) {
      prev.text = prev.text ? `${prev.text} ${line}` : line
    } else {
      blocks.push({ kind: 'paragraph', text: line })
    }
  }

  return blocks
}

/** Render a string with inlined bold segments. */
function Inline({ s }: { s: string }) {
  const parts = s.split(INLINE_BOLD)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? <strong key={i}>{part}</strong> : <span key={i}>{part}</span>,
      )}
    </>
  )
}

interface Props {
  text: string
}

export function ChatMessageBody({ text }: Props) {
  // Cheap short-circuit: if there is no markdown syntax it's a plain string render.
  if (!/(\*\*|\d+\.\s|^[*•-]\s)/m.test(text)) {
    return <p className="chat-plain"><Inline s={text} /></p>
  }

  const blocks = parseBlocks(text)

  return (
    <div className="chat-rich">
      {blocks.map((block, i) => {
        if (block.kind === 'number') {
          return (
            <div key={i} className="chat-rich-item chat-rich-number">
              <span className="chat-rich-num">{block.number}</span>
              <div className="chat-rich-body">
                {block.lead && <strong className="chat-rich-lead">{block.lead}:</strong>}
                {block.text && <p className="chat-rich-text"><Inline s={block.text} /></p>}
              </div>
            </div>
          )
        }
        if (block.kind === 'bullet') {
          return (
            <div key={i} className="chat-rich-item chat-rich-bullet">
              <span className="chat-rich-dot" aria-hidden="true" />
              <div className="chat-rich-body">
                {block.lead && <strong className="chat-rich-lead">{block.lead}:</strong>}
                {' '}<Inline s={block.text} />
              </div>
            </div>
          )
        }
        return (
          <p key={i} className="chat-rich-text chat-rich-paragraph">
            <Inline s={block.text} />
          </p>
        )
      })}
    </div>
  )
}

export default ChatMessageBody
