/** Virtualised chat log: older pages prepend without moving the view, new items stick to the bottom. */
import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { Virtuoso } from 'react-virtuoso'
import { ChatEntry } from './entries'
import type { ChatItem } from './chatModel'

export const START_INDEX = 1_000_000
/** Distance from the bottom within which new items keep the view pinned. */
export const FOLLOW_PX = 48

interface Track {
  items: ChatItem[]
  index: number
}

/** Virtuoso's firstItemIndex after the list changed: down by items added in front, up by items trimmed off the front. */
export function nextFirstIndex(prev: Track, items: ChatItem[]): number {
  if (prev.items.length === 0 || items.length === 0) return prev.index
  const oldFirst = prev.items[0].eventId
  if (items.some((i) => i.eventId === oldFirst)) return prev.index - items.filter((i) => i.eventId < oldFirst).length
  const newFirst = items[0].eventId
  return prev.index + prev.items.filter((i) => i.eventId < newFirst).length
}

export interface ChatLogProps {
  items: ChatItem[]
  hasMoreBefore: boolean
  loadingOlder: boolean
  onLoadOlder: () => void
}

export function ChatLog({ items, hasMoreBefore, loadingOlder, onLoadOlder }: ChatLogProps) {
  const [track, setTrack] = useState<Track>({ items, index: START_INDEX })
  let index = track.index
  if (track.items !== items) {
    index = nextFirstIndex(track, items)
    setTrack({ items, index })
  }
  return (
    <Virtuoso
      style={{ flexGrow: 1, minHeight: 0 }}
      data={items}
      firstItemIndex={index}
      initialTopMostItemIndex={Math.max(items.length - 1, 0)}
      followOutput={(atBottom) => (atBottom ? 'auto' : false)}
      atBottomThreshold={FOLLOW_PX}
      startReached={onLoadOlder}
      computeItemKey={(_i, item) => item.eventId}
      itemContent={(_i, item) => <ChatEntry item={item} />}
      components={{
        Header: () => (
          <Box sx={{ px: 2, py: 1 }}>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              {hasMoreBefore || loadingOlder ? 'Loading older messages' : 'Start of history'}
            </Typography>
          </Box>
        ),
      }}
    />
  )
}
