import { describe, expect, it } from 'vitest'

import {
  PTY_DESKTOP_OPEN_SOCKET_RECONNECT_AFTER_MS,
  PTY_MOBILE_OPEN_SOCKET_RECONNECT_AFTER_MS,
  PTY_RECONNECT_BASE_MS,
  PTY_RECONNECT_MAX_ATTEMPTS,
  PTY_RECONNECT_MAX_MS,
  ptyReconnectDelayMs,
  shouldBlockPtyInput,
  shouldReconnectPtyOnPageResume
} from './pty-reconnect'

describe('shouldReconnectPtyOnPageResume', () => {
  it('reconnects a missing socket when the active page becomes visible', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: null,
        ptyState: 'reconnecting'
      })
    ).toBe(true)
  })

  it('reconnects closed or closing sockets on visible resume', () => {
    for (const socketReadyState of [2, 3]) {
      expect(
        shouldReconnectPtyOnPageResume({
          isActive: true,
          visibilityState: 'visible',
          online: true,
          socketReadyState,
          ptyState: 'reconnecting'
        })
      ).toBe(true)
    }
  })

  it('does not restart the ladder on focus/online after it gave up; only the Reconnect button does', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: 3,
        ptyState: 'closed',
        reconnectGaveUp: true
      })
    ).toBe(false)
  })

  it('does not reconnect an open socket on visible resume', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: 1,
        ptyState: 'open'
      })
    ).toBe(false)
  })

  it('reconnects a still-connecting socket when the page is already in reconnecting state', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: 0,
        ptyState: 'reconnecting'
      })
    ).toBe(true)
  })

  it('does not reconnect while the page is hidden', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'hidden',
        online: true,
        socketReadyState: 3,
        ptyState: 'reconnecting'
      })
    ).toBe(false)
  })

  it('defers reconnect while offline', () => {
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: false,
        socketReadyState: 3,
        ptyState: 'reconnecting'
      })
    ).toBe(false)
  })

  it('does not fire a redundant reconnect while a connect is in flight (wsRef not yet assigned)', () => {
    // The async socket-open IIFE has begun but not yet assigned wsRef, so
    // socketReadyState reads null. Without the connectInFlight guard this
    // would return true and double-connect.
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: null,
        ptyState: 'connecting',
        connectInFlight: true
      })
    ).toBe(false)
  })

  it('still reconnects an in-flight connect when the page already believes it is closed', () => {
    // A stuck attempt the user is actively trying to recover (manual reconnect
    // or a closed state) must not be suppressed by the in-flight guard.
    expect(
      shouldReconnectPtyOnPageResume({
        isActive: true,
        visibilityState: 'visible',
        online: true,
        socketReadyState: null,
        ptyState: 'closed',
        connectInFlight: true
      })
    ).toBe(true)
  })
})

describe('shouldBlockPtyInput', () => {
  it('allows input only while the PTY socket is open', () => {
    expect(shouldBlockPtyInput('open')).toBe(false)
    expect(shouldBlockPtyInput('connecting')).toBe(true)
    expect(shouldBlockPtyInput('reconnecting')).toBe(true)
    expect(shouldBlockPtyInput('closed')).toBe(true)
    expect(shouldBlockPtyInput('ended')).toBe(true)
  })
})

describe('ptyReconnectDelayMs', () => {
  it('doubles from the base on each attempt and clamps at the cap', () => {
    expect(Array.from({ length: PTY_RECONNECT_MAX_ATTEMPTS }, (_, i) => ptyReconnectDelayMs(i + 1))).toEqual([
      250, 500, 1000, 2000, 3000
    ])
    expect(ptyReconnectDelayMs(1)).toBe(PTY_RECONNECT_BASE_MS)
    expect(ptyReconnectDelayMs(99)).toBe(PTY_RECONNECT_MAX_MS)
  })
})

describe('shouldReconnectPtyOnPageResume — stale OPEN socket after background', () => {
  // A WebSocket can report OPEN after a mobile app switch or radio handoff
  // while the TCP path underneath is dead. No `onclose` fires, so without this
  // the viewer sits on a frozen terminal indefinitely.
  const openSocket = {
    isActive: true,
    visibilityState: 'visible' as const,
    online: true,
    socketReadyState: 1, // WS_OPEN
    ptyState: 'open' as const
  }

  it('leaves a healthy open socket alone when the tab was never hidden', () => {
    expect(shouldReconnectPtyOnPageResume({ ...openSocket, hiddenAtMs: null })).toBe(false)
    // No timestamp at all — nothing to measure, so keep the old behaviour.
    expect(shouldReconnectPtyOnPageResume(openSocket)).toBe(false)
  })

  it('recycles an open socket after a mobile-length background interval', () => {
    const hiddenAtMs = 10_000
    expect(
      shouldReconnectPtyOnPageResume({
        ...openSocket,
        hiddenAtMs,
        nowMs: hiddenAtMs + PTY_MOBILE_OPEN_SOCKET_RECONNECT_AFTER_MS,
        mobileLike: true
      })
    ).toBe(true)
  })

  it('does not recycle on mobile below the threshold', () => {
    const hiddenAtMs = 10_000
    expect(
      shouldReconnectPtyOnPageResume({
        ...openSocket,
        hiddenAtMs,
        nowMs: hiddenAtMs + PTY_MOBILE_OPEN_SOCKET_RECONNECT_AFTER_MS - 1,
        mobileLike: true
      })
    ).toBe(false)
  })

  it('holds desktop to a much longer threshold than mobile', () => {
    const hiddenAtMs = 10_000
    // The mobile threshold must NOT recycle a desktop socket: a brief tab
    // switch rarely kills a healthy desktop connection, and reconnecting on
    // every alt-tab would churn the terminal.
    expect(
      shouldReconnectPtyOnPageResume({
        ...openSocket,
        hiddenAtMs,
        nowMs: hiddenAtMs + PTY_MOBILE_OPEN_SOCKET_RECONNECT_AFTER_MS,
        mobileLike: false
      })
    ).toBe(false)
    expect(
      shouldReconnectPtyOnPageResume({
        ...openSocket,
        hiddenAtMs,
        nowMs: hiddenAtMs + PTY_DESKTOP_OPEN_SOCKET_RECONNECT_AFTER_MS,
        mobileLike: false
      })
    ).toBe(true)
    expect(PTY_DESKTOP_OPEN_SOCKET_RECONNECT_AFTER_MS).toBeGreaterThan(
      PTY_MOBILE_OPEN_SOCKET_RECONNECT_AFTER_MS
    )
  })

  it('never overrides the give-up latch or an ended session', () => {
    const staleOpen = {
      ...openSocket,
      hiddenAtMs: 10_000,
      nowMs: 10_000 + PTY_DESKTOP_OPEN_SOCKET_RECONNECT_AFTER_MS * 2,
      mobileLike: true
    }
    // The overlay says retries stopped — a stale socket must not silently
    // restart the ladder behind the user's back.
    expect(shouldReconnectPtyOnPageResume({ ...staleOpen, reconnectGaveUp: true })).toBe(false)
    expect(shouldReconnectPtyOnPageResume({ ...staleOpen, ptyState: 'ended' })).toBe(false)
    // ...but with neither guard set, the same input does recycle.
    expect(shouldReconnectPtyOnPageResume(staleOpen)).toBe(true)
  })

  it('still refuses while hidden or offline', () => {
    const staleOpen = {
      ...openSocket,
      hiddenAtMs: 10_000,
      nowMs: 10_000 + PTY_DESKTOP_OPEN_SOCKET_RECONNECT_AFTER_MS * 2,
      mobileLike: true
    }
    expect(shouldReconnectPtyOnPageResume({ ...staleOpen, visibilityState: 'hidden' })).toBe(false)
    expect(shouldReconnectPtyOnPageResume({ ...staleOpen, online: false })).toBe(false)
    expect(shouldReconnectPtyOnPageResume({ ...staleOpen, isActive: false })).toBe(false)
  })
})
