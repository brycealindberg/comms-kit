// lib/message-buffer.js

class MessageBuffer {
  /**
   * @param {Object} opts
   * @param {number} opts.maxPerChannel - Max messages per channel (default 500)
   * @param {number} opts.maxTotal - Max total messages across all channels (default 10000)
   */
  constructor({ maxPerChannel = 500, maxTotal = 10000 } = {}) {
    this.maxPerChannel = maxPerChannel;
    this.maxTotal = maxTotal;
    /** @type {Map<string, Array>} channel_id -> messages */
    this.channels = new Map();
    /** @type {Map<string, number>} channel_id -> insertion count. A late
     * reply into an already-full channel can leave BOTH message_count and
     * last_activity (the two fields /channels exposes) unchanged -- it
     * evicts the oldest message to stay at the cap, and it is not the
     * newest ts either. This counts every REAL insertion (never a
     * deduped repeat) regardless of where it landed, so a caller (the
     * relay's poll_once) has a third signal that something changed even
     * when those two did not (Codex review 2026-10-04, issue 1). */
    this.revisions = new Map();
  }

  /**
   * Add a message to the buffer. Messages are kept sorted by timestamp.
   * Evicts oldest when per-channel or total cap is hit.
   */
  add(channelId, message) {
    if (!this.channels.has(channelId)) {
      this.channels.set(channelId, []);
    }
    const msgs = this.channels.get(channelId);

    // Deduplicate by ts
    if (msgs.some(m => m.ts === message.ts)) return;

    msgs.push(message);
    msgs.sort((a, b) => parseFloat(a.ts) - parseFloat(b.ts));
    this.revisions.set(channelId, (this.revisions.get(channelId) || 0) + 1);

    // Per-channel cap: evict oldest
    while (msgs.length > this.maxPerChannel) {
      msgs.shift();
    }

    // Total cap: evict oldest across all channels
    while (this.totalMessages() > this.maxTotal) {
      this._evictOldest();
    }
  }

  /**
   * Add multiple messages at once.
   */
  addBatch(channelId, messages) {
    for (const msg of messages) {
      this.add(channelId, msg);
    }
  }

  /**
   * Get messages for a channel, optionally filtered.
   */
  getMessages(channelId, { since, limit } = {}) {
    const msgs = this.channels.get(channelId) || [];
    let filtered = msgs;

    if (since) {
      const sinceFloat = parseFloat(since);
      filtered = filtered.filter(m => parseFloat(m.ts) > sinceFloat);
    }

    if (limit && limit > 0) {
      filtered = filtered.slice(-limit);
    }

    return filtered;
  }

  /**
   * How many real (non-deduped) insertions this channel has ever had. 0
   * for a channel never seen -- same "never observed" meaning as an empty
   * message array.
   */
  getRevision(channelId) {
    return this.revisions.get(channelId) || 0;
  }

  /**
   * Get stats for all channels in the buffer.
   */
  getChannelStats() {
    const stats = [];
    for (const [channelId, msgs] of this.channels.entries()) {
      stats.push({
        channel_id: channelId,
        message_count: msgs.length,
        oldest: msgs.length > 0 ? msgs[0].ts : null,
        newest: msgs.length > 0 ? msgs[msgs.length - 1].ts : null,
      });
    }
    return stats;
  }

  totalMessages() {
    let total = 0;
    for (const msgs of this.channels.values()) {
      total += msgs.length;
    }
    return total;
  }

  _evictOldest() {
    let oldestTs = Infinity;
    let oldestChannel = null;

    for (const [channelId, msgs] of this.channels.entries()) {
      if (msgs.length > 0 && parseFloat(msgs[0].ts) < oldestTs) {
        oldestTs = parseFloat(msgs[0].ts);
        oldestChannel = channelId;
      }
    }

    if (oldestChannel) {
      this.channels.get(oldestChannel).shift();
      if (this.channels.get(oldestChannel).length === 0) {
        this.channels.delete(oldestChannel);
      }
    }
  }

  clear() {
    this.channels.clear();
  }
}

module.exports = { MessageBuffer };
