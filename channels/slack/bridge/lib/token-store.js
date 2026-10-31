// lib/token-store.js
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const ALGORITHM = 'aes-256-gcm';

class TokenStore {
  constructor(configPath) {
    this.configPath = configPath || path.join(__dirname, '..', 'config.json');
    this.workspaces = {};
    this._load();
  }

  _getKey() {
    const key = process.env.ENCRYPTION_KEY || '';
    if (!key || key.length < 32) {
      throw new Error('ENCRYPTION_KEY must be at least 32 characters');
    }
    return Buffer.from(key.slice(0, 32), 'utf-8');
  }

  _encrypt(text) {
    const key = this._getKey();
    const iv = crypto.randomBytes(16);
    const cipher = crypto.createCipheriv(ALGORITHM, key, iv);
    let encrypted = cipher.update(text, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    const authTag = cipher.getAuthTag().toString('hex');
    return `${iv.toString('hex')}:${authTag}:${encrypted}`;
  }

  _decrypt(data) {
    const key = this._getKey();
    const [ivHex, authTagHex, encrypted] = data.split(':');
    const iv = Buffer.from(ivHex, 'hex');
    const authTag = Buffer.from(authTagHex, 'hex');
    const decipher = crypto.createDecipheriv(ALGORITHM, key, iv);
    decipher.setAuthTag(authTag);
    let decrypted = decipher.update(encrypted, 'hex', 'utf8');
    decrypted += decipher.final('utf8');
    return decrypted;
  }

  _load() {
    if (!fs.existsSync(this.configPath)) {
      this.workspaces = {};
      return;
    }
    try {
      const raw = JSON.parse(fs.readFileSync(this.configPath, 'utf-8'));
      for (const [teamId, ws] of Object.entries(raw.workspaces || {})) {
        this.workspaces[teamId] = {
          name: ws.name,
          token: this._decrypt(ws.token),
          cookie: this._decrypt(ws.cookie),
          status: ws.status || 'connected',
          added_at: ws.added_at,
          last_valid: ws.last_valid,
        };
      }
    } catch (e) {
      console.error(`Failed to load config: ${e.message}`);
      this.workspaces = {};
    }
  }

  _save() {
    const serialized = { workspaces: {} };
    for (const [teamId, ws] of Object.entries(this.workspaces)) {
      serialized.workspaces[teamId] = {
        name: ws.name,
        token: this._encrypt(ws.token),
        cookie: this._encrypt(ws.cookie),
        status: ws.status,
        added_at: ws.added_at,
        last_valid: ws.last_valid,
      };
    }
    // Private before the write: an older file can still carry a mode other users can read.
    if (fs.existsSync(this.configPath)) fs.chmodSync(this.configPath, 0o600);
    fs.writeFileSync(this.configPath, JSON.stringify(serialized, null, 2), { mode: 0o600 });
  }

  setWorkspace(teamId, { name, token, cookie }) {
    this.workspaces[teamId] = {
      name,
      token,
      cookie,
      status: 'connected',
      added_at: new Date().toISOString(),
      last_valid: new Date().toISOString(),
    };
    this._save();
  }

  getWorkspace(teamId) {
    return this.workspaces[teamId] || null;
  }

  listWorkspaces() {
    return Object.entries(this.workspaces).map(([teamId, ws]) => ({
      team_id: teamId,
      ...ws,
    }));
  }

  updateStatus(teamId, status) {
    if (this.workspaces[teamId]) {
      this.workspaces[teamId].status = status;
      if (status === 'connected') {
        this.workspaces[teamId].last_valid = new Date().toISOString();
      }
      this._save();
    }
  }

  /**
   * Update only the token for an existing workspace.
   * Preserves cookie, name, and added_at. Resets status to connected.
   */
  updateToken(teamId, newToken) {
    if (!this.workspaces[teamId]) return false;
    this.workspaces[teamId].token = newToken;
    this.workspaces[teamId].status = 'connected';
    this.workspaces[teamId].last_valid = new Date().toISOString();
    this._save();
    return true;
  }

  removeWorkspace(teamId) {
    delete this.workspaces[teamId];
    this._save();
  }
}

module.exports = { TokenStore };
