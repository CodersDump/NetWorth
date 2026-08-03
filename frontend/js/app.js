let userPool = null;
    if (COGNITO_USER_POOL_ID && !COGNITO_USER_POOL_ID.includes('REPLACE_ME')) {
      userPool = new AmazonCognitoIdentity.CognitoUserPool({
        UserPoolId: COGNITO_USER_POOL_ID, ClientId: COGNITO_CLIENT_ID
      });
    }
    let authSession = null;   // { idToken (raw string), claims (decoded), _pendingUser (mid-challenge) }

    function getAuthHeaders() {
      return authSession ? { 'Authorization': `Bearer ${authSession.idToken}` } : {};
    }
    function isLoggedIn() { return !!authSession; }

    // ---------- token freshness ----------
    // Cognito ID tokens expire after 1 hour. Before this existed, the
    // token captured at login was reused verbatim forever, so a session
    // that started at the beginning of a tournament was already dead by
    // the time later matches got recorded - the Authorizer returned 401
    // and the UI reported it in a quiet one-line message that is very
    // easy to miss on a phone mid-game. THIS is why a match "didn't
    // register" and then went through fine later from a fresh session.
    //
    // getSession() from amazon-cognito-identity-js transparently uses the
    // stored refresh token (valid ~30 days) to mint a new ID token when
    // the current one is expired, so this costs nothing on the happy path.
    const TOKEN_REFRESH_MARGIN_SEC = 300;  // refresh when under 5 minutes remain, not at the moment of expiry

    function tokenSecondsRemaining() {
      if (!authSession || !authSession.claims || !authSession.claims.exp) return 0;
      return authSession.claims.exp - Math.floor(Date.now() / 1000);
    }

    function ensureFreshToken(force = false) {
      return new Promise((resolve) => {
        if (!authSession) { resolve(false); return; }
        if (!force && tokenSecondsRemaining() > TOKEN_REFRESH_MARGIN_SEC) { resolve(true); return; }
        let user = authSession.cognitoUser;
        if (!user && userPool) {
          // Restored-from-sessionStorage sessions have no cognitoUser
          // attached, so fall back to whoever the SDK has stored - but
          // ONLY if it is the same person. On a shared device these can
          // legitimately differ, and silently refreshing into someone
          // else's identity would be far worse than not refreshing.
          const stored = userPool.getCurrentUser();
          const expected = authSession.claims['cognito:username'] || authSession.claims.email;
          if (stored && expected && stored.getUsername() === expected) user = stored;
        }
        if (!user) { resolve(false); return; }
        user.getSession((err, session) => {
          if (err || !session || !session.isValid()) { resolve(false); return; }
          const idToken = session.getIdToken();
          authSession = { idToken: idToken.getJwtToken(), claims: idToken.payload, cognitoUser: user };
          resolve(true);
        });
      });
    }

    /**
     * Every authenticated call should go through this rather than raw
     * fetch. It refreshes a near-expired token first, and if the server
     * still rejects with 401/403 it forces one more refresh and retries
     * exactly once before giving up - so a stale token costs a retry,
     * never a silently lost match.
     * Returns { res, data, error } where error is an already-readable
     * string (never "undefined") when something went wrong.
     */
    async function authedFetch(url, options = {}) {
      const send = async () => {
        const headers = { ...(options.headers || {}), ...getAuthHeaders() };
        const res = await fetch(url, { ...options, headers });
        let data = null;
        try { data = await res.json(); } catch (_) { data = null; }
        return { res, data };
      };

      await ensureFreshToken();
      let { res, data } = await send();

      let sessionExpired = false;
      if ((res.status === 401 || res.status === 403) && authSession) {
        const refreshed = await ensureFreshToken(true);
        if (refreshed) ({ res, data } = await send());
        // Still rejected after a forced refresh - or the refresh token
        // itself was dead so we couldn't even try. Either way this session
        // cannot be recovered silently; the caller should prompt re-login
        // rather than showing a dead-end "Not authorized".
        if (!res.ok && (res.status === 401 || res.status === 403)) sessionExpired = true;
      }

      return { res, data, error: res.ok ? null : describeApiError(res, data), sessionExpired };
    }

    /** Turns any failure shape into something a human can act on. The old
     *  code read `data.error` unconditionally, but API Gateway's own
     *  Authorizer rejections use `message`, and 401s often have no body at
     *  all - both rendered the literal text "Error: undefined". */
    // Belt and braces on top of the per-request refresh: keep the token
    // warm in the background so it is almost never stale at the moment
    // you actually need it. The visibilitychange hook matters most in
    // practice - a phone locked in a bag between games is exactly how a
    // session goes cold, and this refreshes the instant it wakes up.
    setInterval(() => { if (authSession) ensureFreshToken(); }, 10 * 60 * 1000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && authSession) ensureFreshToken();
    });

    function describeApiError(res, data) {
      if (data && (data.error || data.message)) return data.error || data.message;
      if (res.status === 401 || res.status === 403) {
        return 'Your session expired. Log out and log back in, then try again.';
      }
      return `Request failed (HTTP ${res.status})`;
    }
    function isSuperAdmin() {
      if (!authSession) return false;
      const groups = authSession.claims['cognito:groups'] || '';
      return (Array.isArray(groups) ? groups : groups.split(',')).includes('SuperAdmin');
    }
    function myPlayerId() { return authSession ? authSession.claims['custom:player_id'] : null; }

    /**
     * "Logged in" and "usable account" are NOT the same thing, and
     * conflating them is what let a half-registered account still see the
     * match-recording form. Two distinct broken states exist:
     *   1. Signed up but never completed a profile  -> no custom:player_id
     *   2. Had a profile, but that player record was later DELETED -> the
     *      claim still sits in the JWT (claims are baked in at login and
     *      don't notice a deletion), pointing at a player that is gone.
     * Case 2 is the one that bit Mayank's friend. Both now resolve to
     * "not linked", and every write-capable form gates on this instead of
     * on isLoggedIn().
     */
    function hasLinkedPlayer() {
      if (!authSession) return false;
      const pid = myPlayerId();
      if (!pid) return false;
      // Don't punish a slow roster fetch: until /players has answered
      // once, assume the link is fine rather than flashing the "not
      // linked" notice at everyone on every page load.
      if (!allPlayers.length) return true;
      return allPlayers.some(p => p.player_id === pid);
    }
    function myRoleInGroup(group) {
      if (isSuperAdmin()) return 'owner';  // SuperAdmin acts with owner-level power everywhere
      const pid = myPlayerId();
      if (!pid || !group || !group.roles) return null;
      return group.roles[pid] || null;
    }
    function canManageGroup(group) {
      const role = myRoleInGroup(group);
      return role === 'owner' || role === 'admin';
    }
    // A group owner/admin can review requests for their own group's members,
    // so they get the Reviews tab too - but the backend scopes what they see
    // and can act on (claim/rename only), and the SuperAdmin-only panels in
    // that tab stay hidden for them (see updateReviewTabScope).
    function ownsAnyGroup() {
      return (allGroups || []).some(g => canManageGroup(g));
    }
    function canReviewRequests() {
      return isSuperAdmin() || ownsAnyGroup();
    }
    // In the Reviews tab, a group owner (non-super) only gets the claim/rename
    // requests panel. Every other section there - finance access, feature
    // settings, quests, events, store - is a SuperAdmin-only control, so hide
    // them. The claim panel is the one holding #settings-requests-list.
    function updateReviewTabScope() {
      const sections = document.querySelectorAll('#tab-review .review-section');
      const showAll = isSuperAdmin();
      sections.forEach(sec => {
        const isRequests = !!sec.querySelector('#settings-requests-list');
        sec.style.display = (showAll || isRequests) ? '' : 'none';
      });
    }

    // ---------- nickname/name display toggle ----------
    // Deliberately zero new API calls: GET /players (and everywhere that
    // reuses that data - rankings, group member lists, the header
    // identity) already returns both name and nickname per player, so
    // flipping this just re-renders already-cached data with the other
    // field prioritized. Match/tournament/substitution selectors always
    // show both regardless of this toggle (disambiguation there isn't a
    // preference, it's a safety requirement). Hall of Fame, achievements,
    // and progress history are NOT yet toggle-aware - the backend
    // currently pre-merges "Nickname (Name)" into one string for those,
    // which would need to change to raw fields for this to reach them too
    // (a separate, larger follow-up, not done here).
    let showNicknameFirst = localStorage.getItem('nw_display_mode') !== 'name';

    function formatPlayerLabel(name, nickname) {
      if (!nickname) return name;
      return showNicknameFirst ? nickname : `${nickname} (${name})`;
    }

    function toggleDisplayMode() {
      showNicknameFirst = !showNicknameFirst;
      localStorage.setItem('nw_display_mode', showNicknameFirst ? 'nickname' : 'name');
      document.getElementById('display-mode-toggle-btn').textContent = showNicknameFirst ? 'Show: Nickname only' : 'Show: Nickname + Name';
      // Re-render everything toggle-aware, unconditionally - not just
      // "if a group happens to be selected right now" or "if you're on
      // that tab" - loadGroupMembers/loadVisiblePlayers already no-op
      // safely with nothing selected / not logged in, so calling them
      // regardless is what actually makes the toggle reach every view
      // consistently instead of only the one you're currently looking at.
      if (typeof loadRankings === 'function') loadRankings();
      if (lastHofData && typeof renderHallOfFame === 'function') renderHallOfFame(lastHofData);
      if (lastBadgesData && typeof renderBadges === 'function') renderBadges(lastBadgesData);
      if (lastDiversityData && typeof renderDiversity === 'function') renderDiversity(lastDiversityData);
      if (lastAttendanceData && typeof renderAttendance === 'function') renderAttendance(lastAttendanceData);
      if (lastHistoryData && typeof renderHistory === 'function') renderHistory(lastHistoryData);
      const groupSel = document.getElementById('group_select');
      if (groupSel && typeof loadGroupMembers === 'function') loadGroupMembers(groupSel.value);
      if (typeof loadVisiblePlayers === 'function') loadVisiblePlayers();
      // The Player Card's name label is toggle-aware too, but it was left
      // out here - so the banner kept the old format until a manual
      // reload. Re-render it in place from the currently-selected player.
      const selId = document.getElementById('profile_player_select')?.value;
      if (selId) {
        const sel = allPlayers.find(p => p.player_id === selId);
        if (sel && typeof renderProfileCardBanner === 'function') renderProfileCardBanner(sel);
      }
      if (typeof updateAuthUI === 'function') updateAuthUI();
    }

    // UPI payment card config: set your (ideally secondary) VPA to make the
    // 'Pay the club' card appear on the Finance tab. Leave as REPLACE_ME to
    // keep the card hidden.
    // Server-driven now (editable from Settings by anyone with finance
    // write access). These are just the in-memory current values; the real
    // source of truth is the finance settings record, fetched on load.
    let UPI_ID = '';
    let UPI_NAME = 'Matchpoint Badminton';
    // Easiest path: export the QR straight from your UPI app (GPay/PhonePe
    // -> your profile -> Share QR -> Save image), then add that image file
    // to frontend/assets/upi-qr.png in the repo and leave this path as-is.
    // If that file isn't there, a QR is generated on the fly from UPI_ID
    // instead (needs a reachable CDN) - either works, the image is simpler
    // and has no external dependency.
    const UPI_QR_IMAGE_BASE = 'assets/upi-qr';   // tries .png, .jpg, .jpeg, .webp in that order - upload whichever format you have, no code change needed

    let allPlayers = [];
    let allGroups = [];

    // ---- Themed modal system: nwConfirm / nwAlert / nwPrompt ----
    // Async replacements for the native browser dialogs so they match the
    // match the app (light + dark via CSS vars). Return Promises.
    function _nwModal({ message, input, defaultValue, okText, cancelText, danger }) {
      return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'nw-modal-overlay';
        const box = document.createElement('div');
        box.className = 'nw-modal';
        const msg = document.createElement('div');
        msg.className = 'nw-modal-msg';
        msg.textContent = message == null ? '' : String(message);
        box.appendChild(msg);
        let field = null;
        if (input) {
          field = document.createElement('input');
          field.className = 'nw-modal-input';
          field.value = defaultValue == null ? '' : String(defaultValue);
          box.appendChild(field);
        }
        const actions = document.createElement('div');
        actions.className = 'nw-modal-actions';
        const cleanup = (val) => {
          overlay.classList.remove('nw-open');
          setTimeout(() => overlay.remove(), 140);
          document.removeEventListener('keydown', onKey);
          resolve(val);
        };
        if (cancelText !== null) {
          const cancel = document.createElement('button');
          cancel.className = 'nw-modal-btn';
          cancel.textContent = cancelText || 'Cancel';
          cancel.onclick = () => cleanup(input ? null : false);
          actions.appendChild(cancel);
        }
        const ok = document.createElement('button');
        ok.className = 'nw-modal-btn ' + (danger ? 'nw-danger' : 'nw-primary');
        ok.textContent = okText || 'OK';
        ok.onclick = () => cleanup(input ? field.value : true);
        actions.appendChild(ok);
        box.appendChild(actions);
        overlay.appendChild(box);
        // Click outside = cancel (for confirm/prompt); alerts have no cancel.
        overlay.addEventListener('mousedown', e => { if (e.target === overlay && cancelText !== null) cleanup(input ? null : false); });
        const onKey = (e) => {
          if (e.key === 'Escape' && cancelText !== null) cleanup(input ? null : false);
          if (e.key === 'Enter') { e.preventDefault(); ok.click(); }
        };
        document.addEventListener('keydown', onKey);
        document.body.appendChild(overlay);
        requestAnimationFrame(() => overlay.classList.add('nw-open'));
        if (field) { field.focus(); field.select(); }
      });
    }
    function nwConfirm(message, opts = {}) {
      return _nwModal({ message, okText: opts.okText || 'Confirm', cancelText: opts.cancelText || 'Cancel', danger: opts.danger });
    }
    function nwAlert(message, opts = {}) {
      return _nwModal({ message, okText: opts.okText || 'OK', cancelText: null });
    }
    function nwPrompt(message, defaultValue = '', opts = {}) {
      return _nwModal({ message, input: true, defaultValue, okText: opts.okText || 'OK', cancelText: opts.cancelText || 'Cancel' });
    }

    // Which account the current Player Card selection belongs to. Compared
    // against myPlayerId() so a selection made by a previous login in the
    // same tab is discarded rather than inherited.
    let profileSelectionOwner = null;

    // ---------- helpers ----------

    function populateSelect(selectEl, items, valueKey, labelKey, placeholder) {
      selectEl.innerHTML = '';
      if (placeholder) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = placeholder;
        selectEl.appendChild(opt);
      }
      const sortedItems = [...items].sort((a, b) =>
        String(a[labelKey] ?? '').localeCompare(String(b[labelKey] ?? ''))
      );
      sortedItems.forEach(item => {
        const opt = document.createElement('option');
        opt.value = item[valueKey];
        opt.textContent = item[labelKey];
        selectEl.appendChild(opt);
      });
    }

    async function loadPlayers() {
      const res = await fetch(`${API_BASE_URL}/players`);
      const data = await res.json();
      allPlayers = data.players || [];
      const labeled = allPlayers.map(p => ({ ...p, label: `${p.name} (${p.nickname}) (${p.rating})` }));
      // Profile-related selects are populated separately by
      // loadVisiblePlayers(), which is group-scoped server-side.
      renderAddPlayersChecklist();
      populateSelect(document.getElementById('delete_player_id'), labeled, 'player_id', 'label', null);
      populateSelect(document.getElementById('log_player_filter'), labeled, 'player_id', 'label', 'All players');
      const oppFilter = document.getElementById('log_opponent_filter');
      if (oppFilter) populateSelect(oppFilter, labeled, 'player_id', 'label', 'Any opponent');
      if (typeof nwPairingRefreshList === 'function') nwPairingRefreshList();
      // Profile-related selects are populated by loadVisiblePlayers()
      // instead (group-scoped server-side) - not here, which would show
      // the full unrestricted roster regardless of who's actually
      // allowed to view whom.
      populateSelect(document.getElementById('fmem_player'), labeled, 'player_id', 'label', '- pick from roster -');
      populateSelect(document.getElementById('fwalk_player'), labeled, 'player_id', 'label', '- not on roster -');
      populateTeamSelects();
      updateAuthUI();  // refresh header identity now that nicknames are available (matters after a restored session)
    }

    async function loadGroups() {
      const res = await fetch(`${API_BASE_URL}/groups`);
      const data = await res.json();
      allGroups = data.groups || [];
      populateSelect(document.getElementById('group_select'), allGroups, 'group_id', 'group_name', null);
      populateSelect(document.getElementById('register_group_id'), allGroups, 'group_id', 'group_name', "Don't add to a group yet");
      populateSelect(document.getElementById('match_group_select'), allGroups, 'group_id', 'group_name', 'None');
      defaultMatchGroup();  // pre-pick the recorder's group once groups are loaded
      populateSelect(document.getElementById('log_group_filter'), allGroups, 'group_id', 'group_name', 'All groups');
      if (typeof nwPairingRefreshList === 'function') nwPairingRefreshList();
      populateSelect(document.getElementById('attendance_group_filter'), allGroups, 'group_id', 'group_name', 'All groups');
      populateSelect(document.getElementById('rankings_scope_select'), allGroups, 'group_id', 'group_name', 'All players');
      // Once someone belongs to a group, default the rankings view to it so
      // they see their group-mates first rather than the whole club. Only
      // when nothing's been chosen yet, so it never fights a manual pick.
      const rankScope = document.getElementById('rankings_scope_select');
      if (rankScope && !rankScope.value) {
        const mine = myGroups();
        if (mine.length) { rankScope.value = mine[0].group_id; if (typeof loadRankings === 'function') loadRankings(); }
      }
      populateSelect(document.getElementById('profile_partnerships_scope_group'), allGroups, 'group_id', 'group_name', 'All plays');
      populateSelect(document.getElementById('hof_group_filter'), allGroups, 'group_id', 'group_name', 'All groups');
      populateSelect(document.getElementById('diversity_group_filter'), allGroups, 'group_id', 'group_name', 'All groups');
      populateSelect(document.getElementById('badges_group_filter'), allGroups, 'group_id', 'group_name', 'All groups');
      const historyScopeSelect = document.getElementById('history_scope_select');
      historyScopeSelect.innerHTML = '<option value="global">Global (all players)</option>' +
        [...allGroups].sort((a, b) => a.group_name.localeCompare(b.group_name))
          .map(g => `<option value="group_${g.group_id}">${g.group_name}</option>`).join('');
      if (allGroups.length) {
        loadGroupMembers(document.getElementById('group_select').value);
      }
    }

    let currentGroupMemberIds = new Set();
    let currentGroupRoles = {};

    async function loadGroupMembers(groupId) {
      const membersEl = document.getElementById('group-members');
      if (!groupId) { membersEl.innerHTML = ''; currentGroupMemberIds = new Set(); currentGroupRoles = {}; renderAddPlayersChecklist(); return; }
      const res = await fetch(`${API_BASE_URL}/groups/${groupId}`);
      const data = await res.json();
      if (!res.ok) { membersEl.textContent = data.error; return; }

      currentGroupMemberIds = new Set((data.members || []).map(m => m.player_id));
      currentGroupRoles = data.roles || {};
      renderAddPlayersChecklist();
      applyGroupDefaultsToForm('group_default_', data.default_tournament_settings);

      const iCanManage = canManageGroup({ roles: currentGroupRoles });
      const deleteBtn = document.getElementById('delete-group-btn');
      if (deleteBtn) deleteBtn.style.display = (!isLoggedIn() || iCanManage) ? 'block' : 'none';

      if (!data.members.length) {
        membersEl.innerHTML = '<p style="font-size:13px;color:#555;">No members yet.</p>';
        return;
      }
      const sortedMembers = [...data.members].sort((a, b) => a.name.localeCompare(b.name));
      const financeRoles = data.finance_roles || {};
      membersEl.innerHTML = '<strong>Members:</strong>' + sortedMembers.map(m => {
        const roleTag = m.role && m.role !== 'member'
          ? `<span style="font-size:11px; opacity:0.75; margin-left:6px; border:1px solid var(--border); border-radius:4px; padding:1px 6px;">${m.role}</span>`
          : '';
        // Guests keep seeing Remove (legacy code-only flow, unchanged); a
        // logged-in user who can't manage this specific group doesn't see
        // it at all, rather than clicking it and getting a confusing 403.
        const removeBtn = (!isLoggedIn() || iCanManage)
          ? `<button onclick="removePlayerFromGroup('${groupId}','${m.player_id}')">Remove</button>` : '';
        const displayLabel = formatPlayerLabel(m.name, m.nickname);
        // Finance access control - owners/admins only. Owners/admins already
        // have full finance implicitly, so this is only meaningful for plain
        // members; shown for everyone so the owner sees the full picture.
        let finCtrl = '';
        if (iCanManage) {
          const isOwnerAdmin = (m.role === 'owner' || m.role === 'admin');
          if (isOwnerAdmin) {
            finCtrl = `<span style="font-size:11px; color:var(--text-secondary); margin-left:8px;">finance: full (${m.role})</span>`;
          } else {
            const cur = financeRoles[m.player_id] || 'none';
            const opt = (v, label) => `<option value="${v}"${cur === v ? ' selected' : ''}>${label}</option>`;
            finCtrl = `<select style="font-size:11px; margin-left:8px; padding:1px 4px;" onchange="setGroupFinanceRole('${groupId}','${m.player_id}', this.value).then(()=>loadGroupMembers('${groupId}'))">`
              + opt('none', 'no finance') + opt('view', 'can view') + opt('write', 'can edit') + opt('delete', 'can delete')
              + '</select>';
          }
        }
        return `<div class="member-row"><span>${displayLabel} (${m.rating})${roleTag}${finCtrl}</span>${removeBtn}</div>`;
      }).join('');

      // Per-group time slots (Stage 4). Owners/admins define the slot list and
      // who plays which slot; that drives slot-scoped finance and dues.
      const slots = data.slots || [];
      const slotMembers = data.slot_members || {};
      const nameOf = (pid) => {
        const mm = (data.members || []).find(x => x.player_id === pid);
        return mm ? formatPlayerLabel(mm.name, mm.nickname) : pid;
      };
      let slotsHtml = '<div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--border);"><strong>Time slots</strong>';
      if (slots.length) {
        slotsHtml += slots.map(s => {
          const assigned = (slotMembers[s] || []).map(nameOf).join(', ') || '<span style="color:var(--text-secondary);">no one assigned</span>';
          const assignBtn = iCanManage ? ` <button style="padding:2px 8px; font-size:11px; margin:0;" onclick="assignSlotMembers('${groupId}','${encodeURIComponent(s)}')">Assign</button>` : '';
          return `<div class="member-row"><span><strong>${s}</strong>: ${assigned}</span>${assignBtn}</div>`;
        }).join('');
      } else {
        slotsHtml += '<p style="font-size:13px; color:var(--text-secondary); margin:4px 0;">No slots defined yet.</p>';
      }
      if (iCanManage) {
        slotsHtml += `<button class="secondary" style="margin-top:8px; padding:4px 10px; font-size:12px;" onclick="manageGroupSlots('${groupId}')">Edit slot list</button>`;
      }
      slotsHtml += '</div>';

      // Ownership + payee (Stage 5). Transfer is owner-only; payee any owner/admin.
      const iAmOwner = (currentGroupRoles[myPlayerId()] === 'owner');
      const payee = data.finance_payee || {};
      if (iCanManage) {
        slotsHtml += '<div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--border);"><strong>Ownership &amp; payments</strong>';
        const payeeName = payee.player_id ? (nameOf(payee.player_id) + (payee.upi_id ? ` (${payee.upi_id})` : '')) : '<span style="color:var(--text-secondary);">not set</span>';
        slotsHtml += `<div class="member-row"><span>Payments collected by: ${payeeName}</span><button style="padding:2px 8px; font-size:11px; margin:0;" onclick="setGroupPayee('${groupId}')">Set payee</button></div>`;
        slotsHtml += '<p style="font-size:12px; color:var(--text-secondary); margin:6px 0 0;">Promote a member to owner/admin from their role control above to add a co-owner.</p>';
        if (iAmOwner) {
          slotsHtml += `<button class="secondary" style="margin-top:8px; padding:4px 10px; font-size:12px;" onclick="transferGroupOwnership('${groupId}')">Transfer ownership</button>`;
        }
        slotsHtml += '</div>';
      }
      membersEl.innerHTML += slotsHtml;
    }

    function applyGroupDefaultsToForm(prefix, settings) {
      if (!settings) return;
      const setIfPresent = (suffix, value) => {
        const el = document.getElementById(`${prefix}${suffix}`);
        if (el && value !== undefined && value !== null) el.value = value;
      };
      setIfPresent('format', settings.format);
      setIfPresent('match_type', settings.match_type);
      setIfPresent('points_to_win', settings.points_to_win);
      setIfPresent('best_of', settings.best_of);
      setIfPresent('pairing_mode', settings.pairing_mode);
    }

    function renderAddPlayersChecklist() {
      const container = document.getElementById('add-players-checklist');
      const available = allPlayers.filter(p => !currentGroupMemberIds.has(p.player_id))
        .sort((a, b) => a.name.localeCompare(b.name));
      if (!available.length) {
        container.innerHTML = '<p style="font-size:13px;color:#555;margin:0;">Everyone registered is already in this group.</p>';
        return;
      }
      container.innerHTML = available.map(p =>
        `<label style="display:block; padding:2px 0;"><input type="checkbox" class="add-player-checkbox" value="${p.player_id}"> ${p.name} (${p.rating})</label>`
      ).join('');
    }

    async function removePlayerFromGroup(groupId, playerId) {
      const confirmText = await nwPrompt('Enter the confirmation code to remove this player from the group:');
      if (!confirmText) return;

      // Logged-in users go through the new Cognito-enforced route (proves
      // real ownership, not just knowledge of a shared code); guests keep
      // using the original code-only route unchanged, for backward
      // compatibility until everyone's been provisioned a login.
      const url = isLoggedIn()
        ? `${API_BASE_URL}/group-member-remove/${groupId}/${playerId}`
        : `${API_BASE_URL}/groups/${groupId}/players/${playerId}`;
      const res = await fetch(url, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify({ confirm: confirmText })
      });
      if (res.ok) {
        loadGroupMembers(groupId);
        loadGroups();
      } else {
        const data = await res.json();
        nwAlert(data.error);
      }
    }

    const TEAM_SELECT_IDS = ['team_a1_select', 'team_a2_select', 'team_b1_select', 'team_b2_select'];

    let teamSelectListenersAttached = false;

    function populateTeamSelects() {
      const isFirstPopulation = TEAM_SELECT_IDS.every(id => !document.getElementById(id).value);

      refreshTeamSelectOptions();

      if (isFirstPopulation && allPlayers.length >= 2) {
        const distinct = allPlayers.slice(0, 4);
        if (distinct[0]) document.getElementById('team_a1_select').value = distinct[0].player_id;
        if (distinct[1]) document.getElementById('team_b1_select').value = distinct[1].player_id;
        if (distinct[2]) document.getElementById('team_a2_select').value = distinct[2].player_id;
        if (distinct[3]) document.getElementById('team_b2_select').value = distinct[3].player_id;
        refreshTeamSelectOptions();
      }

      if (!teamSelectListenersAttached) {
        TEAM_SELECT_IDS.forEach(id => {
          document.getElementById(id).addEventListener('change', () => refreshTeamSelectOptions());
        });
        teamSelectListenersAttached = true;
      }

      applyMatchTypeVisibility();
    }

    let currentMatchGroupMembers = null;

    function refreshTeamSelectOptions() {
      const currentValues = {};
      TEAM_SELECT_IDS.forEach(id => { currentValues[id] = document.getElementById(id).value; });

      const pool = currentMatchGroupMembers || allPlayers;

      TEAM_SELECT_IDS.forEach(id => {
        const selectEl = document.getElementById(id);
        const excludedElsewhere = TEAM_SELECT_IDS
          .filter(otherId => otherId !== id)
          .map(otherId => currentValues[otherId])
          .filter(Boolean);

        const available = pool.filter(p => !excludedElsewhere.includes(p.player_id));
        const labeled = available.map(p => ({ ...p, label: `${p.name} (${p.rating})` }));
        populateSelect(selectEl, labeled, 'player_id', 'label', null);

        if (currentValues[id] && available.some(p => p.player_id === currentValues[id])) {
          selectEl.value = currentValues[id];
        }
      });
    }

    function applyMatchTypeVisibility() {
      const isDoubles = document.getElementById('match_type_select').value === 'doubles';
      document.querySelectorAll('.doubles-only').forEach(el => {
        el.style.display = isDoubles ? 'inline-block' : 'none';
      });
    }

    document.getElementById('match_type_select').addEventListener('change', () => {
      applyMatchTypeVisibility();
      refreshTeamSelectOptions();
    });

    let randomizeTeamsRequestId = 0;

    async function updateMatchGroupCache() {
      const requestId = ++randomizeTeamsRequestId;
      const groupId = document.getElementById('match_group_select').value;
      if (!groupId) {
        currentMatchGroupMembers = null;
        return requestId;
      }
      try {
        const res = await fetch(`${API_BASE_URL}/groups/${groupId}`);
        const data = await res.json();
        if (requestId !== randomizeTeamsRequestId) return requestId; // superseded, discard
        if (res.ok && data.members) currentMatchGroupMembers = data.members;
        else currentMatchGroupMembers = null;
      } catch (err) {
        // leave cache as-is on failure
      }
      return requestId;
    }

    async function randomizeTeams(showAlertOnFail) {
      const requestId = await updateMatchGroupCache();
      if (requestId !== randomizeTeamsRequestId) return; // superseded by a newer call

      const isDoubles = document.getElementById('match_type_select').value === 'doubles';
      const needed = isDoubles ? 4 : 2;
      const groupId = document.getElementById('match_group_select').value;
      const pool = currentMatchGroupMembers || allPlayers;


      if (pool.length < needed) {
        ['team_a1_select', 'team_a2_select', 'team_b1_select', 'team_b2_select'].forEach(id => {
          document.getElementById(id).value = '';
        });
        if (showAlertOnFail) {
          nwAlert(`Need at least ${needed} players${groupId ? ' in this group' : ''} to randomize.`);
        }
        return;
      }
      const shuffled = [...pool].sort(() => Math.random() - 0.5).slice(0, needed);
      document.getElementById('team_a1_select').value = shuffled[0].player_id;
      document.getElementById('team_b1_select').value = shuffled[1].player_id;
      if (isDoubles) {
        document.getElementById('team_a2_select').value = shuffled[2].player_id;
        document.getElementById('team_b2_select').value = shuffled[3].player_id;
      }
      refreshTeamSelectOptions();
    }

    document.getElementById('randomize-teams-btn').addEventListener('click', () => randomizeTeams(true));
    document.getElementById('match_group_select').addEventListener('change', () => randomizeTeams(false));

    // ---------- live point-by-point scoring ----------

    let livePointLog = [];
    let currentTournamentData = null;

    function isGameOver(a, b, target) {
      const cap = target + 9;
      const hi = Math.max(a, b), lo = Math.min(a, b);
      if (hi >= cap) return true;
      if (hi >= target && (hi - lo) >= 2) return true;
      return false;
    }

    function updateLiveScoreDisplay() {
      const a = livePointLog.filter(p => p === 'A').length;
      const b = livePointLog.filter(p => p === 'B').length;
      const target = parseInt(document.getElementById('points_to_win_select').value, 10);
      const over = isGameOver(a, b, target);

      let display = `${a} - ${b}`;
      if (over) {
        display += a > b ? ' - Team A wins the game' : ' - Team B wins the game';
      } else {
        if (a >= target - 1 && a > b) display += ' (match point A)';
        if (b >= target - 1 && b > a) display += ' (match point B)';
      }
      document.getElementById('live-score-display').textContent = display;
      document.getElementById('score_a').value = a;
      document.getElementById('score_b').value = b;

      document.getElementById('live-point-a-btn').disabled = over;
      document.getElementById('live-point-b-btn').disabled = over;
      updateSplitScreenScores(a, b, over);
    }

    document.getElementById('points_to_win_select').addEventListener('change', () => {
      if (document.getElementById('live_scoring_toggle').checked) {
        updateLiveScoreDisplay();
      }
    });

    document.getElementById('live_scoring_toggle').addEventListener('change', (e) => {
      const useLive = e.target.checked;
      document.getElementById('live-score-section').style.display = useLive ? 'block' : 'none';
      document.getElementById('manual-score-section').style.display = useLive ? 'none' : 'flex';
      document.getElementById('score_a').readOnly = useLive;
      document.getElementById('score_b').readOnly = useLive;
      if (useLive) {
        livePointLog = [];
        updateLiveScoreDisplay();
      }
    });

    document.getElementById('live-point-a-btn').addEventListener('click', () => {
      const target = parseInt(document.getElementById('points_to_win_select').value, 10);
      const a = livePointLog.filter(p => p === 'A').length;
      const b = livePointLog.filter(p => p === 'B').length;
      if (isGameOver(a, b, target)) return;
      livePointLog.push('A');
      updateLiveScoreDisplay();
    });

    document.getElementById('live-point-b-btn').addEventListener('click', () => {
      const target = parseInt(document.getElementById('points_to_win_select').value, 10);
      const a = livePointLog.filter(p => p === 'A').length;
      const b = livePointLog.filter(p => p === 'B').length;
      if (isGameOver(a, b, target)) return;
      livePointLog.push('B');
      updateLiveScoreDisplay();
    });

    document.getElementById('live-undo-btn').addEventListener('click', () => {
      livePointLog.pop();
      updateLiveScoreDisplay();
    });

    document.getElementById('live-reset-btn').addEventListener('click', () => {
      livePointLog = [];
      updateLiveScoreDisplay();
    });

    // ---------- split-screen live scoring ----------

    function getTeamDisplayName(selectId) {
      const el = document.getElementById(selectId);
      const opt = el.selectedOptions[0];
      return opt ? opt.textContent : '';
    }

    function getSplitTeamNames() {
      const isDoubles = document.getElementById('match_type_select').value === 'doubles';
      const a1 = getTeamDisplayName('team_a1_select');
      const a2 = isDoubles ? getTeamDisplayName('team_a2_select') : '';
      const b1 = getTeamDisplayName('team_b1_select');
      const b2 = isDoubles ? getTeamDisplayName('team_b2_select') : '';
      return {
        a: [a1, a2].filter(Boolean).join(' & ') || 'Team A',
        b: [b1, b2].filter(Boolean).join(' & ') || 'Team B'
      };
    }

    let splitScreenConfig = null;
    let splitScreenMatchKey = null;

    function updateSplitScreenScores(a, b, over) {
      const overlay = document.getElementById('split-screen-overlay');
      if (overlay.style.display !== 'flex') return;
      document.getElementById('split-score-a').textContent = a;
      document.getElementById('split-score-b').textContent = b;
      document.getElementById('split-zone-a').style.opacity = over ? '0.6' : '1';
      document.getElementById('split-zone-b').style.opacity = over ? '0.6' : '1';
    }

    function openSplitScreenGeneric(config) {
      splitScreenConfig = config;
      document.getElementById('split-name-a').textContent = config.nameA;
      document.getElementById('split-name-b').textContent = config.nameB;
      document.getElementById('split-screen-overlay').style.display = 'flex';
      const score = config.getScore();
      updateSplitScreenScores(score.a, score.b, score.over || false);
    }

    function closeSplitScreen() {
      document.getElementById('split-screen-overlay').style.display = 'none';
      splitScreenConfig = null;
      splitScreenMatchKey = null;
    }

    function openSplitScreen() {
      splitScreenMatchKey = null;
      const names = getSplitTeamNames();
      openSplitScreenGeneric({
        nameA: names.a,
        nameB: names.b,
        getScore: () => {
          const target = parseInt(document.getElementById('points_to_win_select').value, 10) || 21;
          const a = livePointLog.filter(p => p === 'A').length;
          const b = livePointLog.filter(p => p === 'B').length;
          return { a, b, over: isGameOver(a, b, target) };
        },
        addPoint: (side) => {
          document.getElementById(side === 'A' ? 'live-point-a-btn' : 'live-point-b-btn').click();
        },
        undo: () => { document.getElementById('live-undo-btn').click(); },
        reset: () => { document.getElementById('live-reset-btn').click(); },
        submit: () => {
          document.getElementById('split_screen_toggle').checked = false;
          closeSplitScreen();
          document.getElementById('match-form').requestSubmit();
        }
      });
    }

    function openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn) {
      splitScreenMatchKey = matchKey;
      openSplitScreenGeneric({
        nameA, nameB,
        getScore: () => {
          const log = getTournamentLiveLog(matchKey);
          const a = log.filter(p => p === 'A').length;
          const b = log.filter(p => p === 'B').length;
          return { a, b, over: isGameOver(a, b, target) };
        },
        addPoint: (side) => { tournamentLivePoint(matchKey, side, target); },
        undo: () => { tournamentUndoPoint(matchKey, target); },
        reset: () => { delete tournamentLiveLogs[matchKey]; updateTournamentLiveDisplay(matchKey, target); },
        submit: () => {
          closeSplitScreen();
          finishFn();
        }
      });
    }

    document.getElementById('split_screen_toggle').addEventListener('change', (e) => {
      if (e.target.checked) openSplitScreen();
      else closeSplitScreen();
    });

    document.getElementById('split-zone-a').addEventListener('click', () => {
      if (splitScreenConfig) splitScreenConfig.addPoint('A');
    });
    document.getElementById('split-zone-b').addEventListener('click', () => {
      if (splitScreenConfig) splitScreenConfig.addPoint('B');
    });
    document.getElementById('split-undo-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      if (splitScreenConfig) splitScreenConfig.undo();
    });
    document.getElementById('split-reset-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      if (splitScreenConfig) splitScreenConfig.reset();
    });
    document.getElementById('split-submit-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      if (splitScreenConfig) splitScreenConfig.submit();
    });
    document.getElementById('split-exit-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      closeSplitScreen();
    });

    // ---------- registration ----------

    document.getElementById('register-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = document.getElementById('name').value;
      const skill_level = document.getElementById('skill_level').value;
      const groupId = document.getElementById('register_group_id').value;
      const resultEl = document.getElementById('result');
      resultEl.textContent = 'Registering...';
      try {
        // No anonymous branch any more - registering a player is now a
        // logged-in action so every new name has someone attached to it.
        if (!isLoggedIn()) { resultEl.textContent = 'Log in to register a player.'; return; }
        const body = { name, skill_level };
        if (groupId) body.group_id = groupId;
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/register-and-join`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        if (res.ok) {
          resultEl.textContent = data.added_to_group
            ? `Registered! Added to ${data.added_to_group}.`
            : `Registered! Player ID: ${data.player_id}`;
          document.getElementById('register_group_id').value = '';
          loadPlayers();
          loadGroups();
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    document.getElementById('bulk-register-btn').addEventListener('click', async () => {
      const namesRaw = document.getElementById('bulk_register_names').value;
      const skill_level = document.getElementById('bulk_register_skill').value;
      const resultEl = document.getElementById('bulk-register-result');
      const names = namesRaw.split('\n').map(n => n.trim()).filter(Boolean);
      if (!names.length) { resultEl.textContent = 'Paste at least one name.'; return; }

      resultEl.textContent = `Registering ${names.length} player(s)...`;
      const registered = [];
      const failed = [];
      for (const name of names) {
        try {
          const { res, data, error } = await authedFetch(`${API_BASE_URL}/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, skill_level })
          });
          if (res.ok) registered.push(name);
          else failed.push(`${name} (${error})`);
        } catch (err) {
          failed.push(`${name} (request failed)`);
        }
      }
      let msg = `Registered ${registered.length} of ${names.length}.`;
      if (failed.length) msg += ` Not added: ${failed.join('; ')}`;
      resultEl.textContent = msg;
      document.getElementById('bulk_register_names').value = '';
      loadPlayers();
    });

    // ---------- delete player ----------

    document.getElementById('delete-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const playerId = document.getElementById('delete_player_id').value.trim();
      const resultEl = document.getElementById('delete-result');

      if (!isLoggedIn()) { resultEl.textContent = 'Log in to delete a player.'; return; }
      const player = allPlayers.find(p => p.player_id === playerId);
      const label = player ? `${player.name} (${player.nickname})` : playerId;

      // One path for everyone, including SuperAdmin. Deleting is now
      // always a request that lands in the approval queue - no
      // confirmation code is prompted for anywhere. A SuperAdmin's own
      // request shows up in their own queue for a one-tap approval, which
      // costs one extra click and buys a complete record of every
      // deletion, who asked, and who approved it.
      const reason = await nwPrompt(`Request deletion of ${label}?\n\nGive a brief reason (optional):`);
      if (reason === null) { resultEl.textContent = 'Cancelled.'; return; }
      resultEl.textContent = 'Sending request...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/action-request`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'delete_player', player_id: playerId, reason })
        });
        resultEl.textContent = res.ok
          ? `Request sent. An admin will review deleting ${label}.`
          : `Error: ${error}`;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    /**
     * Renaming is self-only now. The old form let you pick ANY player from
     * a dropdown and rename them using the shared code - both a
     * permissions hole and a way for a name to change with nobody
     * accountable. The target is now implicit (it's you) and the change
     * goes through the approval queue.
     */
    function prefillEditForm() {
      const me = allPlayers.find(p => p.player_id === myPlayerId());
      const form = document.getElementById('edit-player-form');
      const locked = document.getElementById('edit-player-locked');
      form.style.display = me ? 'block' : 'none';
      locked.style.display = me ? 'none' : 'block';
      if (!me) return;
      document.getElementById('edit_player_name').value = me.name || '';
      document.getElementById('edit_player_nickname').value = me.nickname || '';
    }

    document.getElementById('edit-player-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = document.getElementById('edit_player_name').value.trim();
      const nickname = document.getElementById('edit_player_nickname').value.trim();
      const resultEl = document.getElementById('edit-player-result');
      if (!name) { resultEl.textContent = 'Name is required.'; return; }
      if (!nickname) { resultEl.textContent = 'Nickname is required.'; return; }
      resultEl.textContent = 'Sending request...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/action-request`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'edit_own_name', name, nickname })
        });
        resultEl.textContent = res.ok
          ? 'Request sent. An admin will review your name change.'
          : `Error: ${error}`;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    // ---------- groups ----------

    document.getElementById('create-group-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const group_name = document.getElementById('group_name').value;
      const resultEl = document.getElementById('create-group-result');
      resultEl.textContent = 'Creating...';
      try {
        // Logged-in users go through the new route, which ties ownership
        // to their real verified identity instead of a client-supplied
        // field; guests keep using the original anonymous route unchanged.
        const url = isLoggedIn() ? `${API_BASE_URL}/group-create` : `${API_BASE_URL}/groups`;
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
          body: JSON.stringify({ group_name })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = `Created group: ${data.group_name}`;
          document.getElementById('group_name').value = '';
          loadGroups();
        } else if (res.status === 402) {
          resultEl.textContent = data.error + ' (Check the Store tab.)';
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    document.getElementById('group_select').addEventListener('change', (e) => {
      loadGroupMembers(e.target.value);
    });

    document.getElementById('refresh-groups-btn').addEventListener('click', loadGroups);

    document.getElementById('delete-group-btn').addEventListener('click', async () => {
      const groupId = document.getElementById('group_select').value;
      const selectedOption = document.getElementById('group_select').selectedOptions[0];
      const resultEl = document.getElementById('delete-group-result');
      if (!groupId) { resultEl.textContent = 'Select a group first.'; return; }

      const label = selectedOption ? selectedOption.textContent : groupId;
      const confirmText = await nwPrompt(`Enter the confirmation code to delete the group "${label}". Players in this group are NOT deleted - only the group itself.`);
      if (!confirmText) { resultEl.textContent = 'Cancelled.'; return; }

      resultEl.textContent = 'Deleting...';
      try {
        const url = isLoggedIn() ? `${API_BASE_URL}/group-delete/${groupId}` : `${API_BASE_URL}/groups/${groupId}`;
        const res = await fetch(url, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
          body: JSON.stringify({ confirm: confirmText })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = `Deleted group: ${data.name}`;
          document.getElementById('group-members').innerHTML = '';
          loadGroups();
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    document.getElementById('add-selected-players-btn').addEventListener('click', async () => {
      const groupId = document.getElementById('group_select').value;
      const resultEl = document.getElementById('group-action-result');
      if (!groupId) { resultEl.textContent = 'Select a group first.'; return; }

      const checked = Array.from(document.querySelectorAll('.add-player-checkbox:checked')).map(cb => cb.value);
      if (!checked.length) { resultEl.textContent = 'Check at least one player to add.'; return; }

      resultEl.textContent = 'Adding...';
      try {
        const url = isLoggedIn() ? `${API_BASE_URL}/group-add-player/${groupId}` : `${API_BASE_URL}/groups/${groupId}/players`;
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
          body: JSON.stringify({ player_ids: checked })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = `Added ${data.added.length} player(s) to group.`;
          loadGroupMembers(groupId);
          loadGroups();
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    document.getElementById('save-group-defaults-btn').addEventListener('click', async () => {
      const groupId = document.getElementById('group_select').value;
      const resultEl = document.getElementById('group-defaults-result');
      if (!groupId) { resultEl.textContent = 'Select a group first.'; return; }

      const settings = {
        format: document.getElementById('group_default_format').value,
        match_type: document.getElementById('group_default_match_type').value,
        points_to_win: document.getElementById('group_default_points_to_win').value,
        best_of: document.getElementById('group_default_best_of').value,
        pairing_mode: document.getElementById('group_default_pairing_mode').value
      };

      resultEl.textContent = 'Saving...';
      try {
        const res = await fetch(`${API_BASE_URL}/groups/${groupId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_tournament_settings: settings })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = 'Saved as default for this group.';
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    // ---------- matches ----------

    document.getElementById('match-quick-add-player-btn').addEventListener('click', async () => {
      const name = await nwPrompt("New player's name:");
      if (!name || !name.trim()) return;
      const groupId = document.getElementById('match_group_select').value;
      const body = { name: name.trim(), skill_level: 'intermediate' };
      if (groupId) body.group_id = groupId;
      const { res, data, error } = await authedFetch(`${API_BASE_URL}/register-and-join`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) { nwAlert('Error: ' + error); return; }
      await loadPlayers();  // refreshes allPlayers + every team-select dropdown
      nwAlert(`${name.trim()} is registered${data.added_to_group ? ' and added to ' + data.added_to_group : ''} - pick them from the team dropdowns below.`);
    });


    /** The groups the logged-in user belongs to, owner or member. */
    function myGroups() {
      const me = myPlayerId();
      if (!me) return [];
      return allGroups.filter(g => (g.member_ids || []).includes(me));
    }

    /** Pre-selects the recorder's group when recording a match, so matches
     *  get attributed by default instead of piling up ungrouped. Picks
     *  their first group; they can still change it (and a SuperAdmin can
     *  set None for a genuinely one-off game). */
    function defaultMatchGroup() {
      const sel = document.getElementById('match_group_select');
      if (!sel || sel.value) return;              // don't override a choice already made
      const mine = myGroups();
      if (mine.length) sel.value = mine[0].group_id;
    }

    // ================= Voice match entry =================
    // Free, client-side, no LLM: the browser's SpeechRecognition does the
    // speech->text (Chrome/Edge/Safari, no key, no cost), and a small rules
    // parser turns e.g. "Aditya and Sohan beat Sourabh and Mayank 21-18" into
    // the match form. It never submits blind - it fills the form and you tap
    // Record, so all the existing validation + safety net still apply.
    // Visibility is gated: SuperAdmins always see it; everyone else only when
    // the "voice_enabled" app setting is on.
    let voiceEnabled = false;
    function applyVoiceVisibility() {
      const w = document.getElementById('nw-voice-wrap');
      if (w) w.style.display = (isSuperAdmin() || voiceEnabled) ? '' : 'none';
    }

    // Phonetic key so Sourabh/Saurabh/Sourav collapse together, while the
    // distinguishing part (C / Devle / T) still separates them via scoring.
    function nwPhon(s) {
       s = (s || '').toLowerCase().replace(/[^a-z]/g, '');
       if (!s) return '';
       const first = s[0];
       const rest = s.slice(1)
         .replace(/[aeiouhwy]/g, '')
         .replace(/z/g, 's').replace(/ck/g, 'k').replace(/c/g, 'k')
         .replace(/ph/g, 'f').replace(/v/g, 'f')
         .replace(/(.)\1+/g, '$1');
       return (first + rest).slice(0, 6);
    }
    function nwLev(a, b) {
       const m = a.length, n = b.length, d = [...Array(m + 1)].map((_, i) => [i, ...Array(n).fill(0)]);
       for (let j = 0; j <= n; j++) d[0][j] = j;
       for (let i = 1; i <= m; i++) for (let j = 1; j <= n; j++)
         d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
       return d[m][n];
    }
    function nwScorePlayer(token, p) {
       const cands = [p.nickname || '', p.name || '', (p.name || '').split(' ')[0]].map(x => x.toLowerCase()).filter(Boolean);
       let best = 0;
       for (const c of cands) {
         if (c === token) best = Math.max(best, 100);
         else if (nwPhon(c) === nwPhon(token)) best = Math.max(best, 80);
         else if (c.startsWith(token) || token.startsWith(c)) best = Math.max(best, 70);
         else { const d = nwLev(nwPhon(c), nwPhon(token)); if (d <= 1) best = Math.max(best, 65); else if (c.includes(token)) best = Math.max(best, 40); }
       }
       const words = token.split(' ').filter(Boolean);
       if (words.length >= 2) {
         const surname = words[words.length - 1];
         const nameWords = (p.name || '').toLowerCase().split(' ').filter(Boolean);
         if (nameWords.some(w => w === surname || nwPhon(w) === nwPhon(surname) || (surname.length > 1 && w.startsWith(surname)))) {
           best = Math.max(best, 95);
         }
       }
       return best;
    }
    function nwMatchPlayerToken(tokenRaw) {
       const token = (tokenRaw || '').trim().toLowerCase();
       if (!token) return { player: null };
       if (['me', 'i', 'my', 'myself'].includes(token)) {
         return { player: allPlayers.find(p => p.player_id === myPlayerId()) || null };
       }
       const scored = allPlayers.map(p => ({ p, s: nwScorePlayer(token, p) })).sort((a, b) => b.s - a.s);
       const top = scored[0], second = scored[1];
       if (!top || top.s < 40) return { player: null };
       if (second && top.s - second.s < 15 && top.s < 100) {
         return { player: null, ambiguous: true, options: [top.p, second.p] };
       }
       return { player: top.p };
    }

    function nwWordsToNums(t) {
       const map = { zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30 };
       t = t.replace(/\bto\b/g, ' to ');
       t = t.replace(/\b(twenty|thirty)\s+(one|two|three|four|five|six|seven|eight|nine)\b/g, (m, a, b) => String(map[a] + map[b]));
       t = t.replace(/\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty)\b/g, m => String(map[m]));
       return t;
    }

    function nwParseMatchTranscript(raw) {
       const text = nwWordsToNums(' ' + raw.toLowerCase().replace(/[.,!?]/g, ' ').replace(/\s+/g, ' ').trim() + ' ');
       const nums = [...text.matchAll(/\b(\d{1,2})\b/g)];
       let s1 = null, s2 = null, namesText = text;
       if (nums.length >= 2) {
         const a = nums[nums.length - 2], b = nums[nums.length - 1];
         s1 = +a[1]; s2 = +b[1];
         namesText = text.slice(0, a.index) + ' ' + text.slice(b.index + b[0].length);
       }
       const loseVerbs = /\b(lost to|lost against|lost)\b/;
       const winVerbs  = /\b(beat|beats|defeated|smashed|thrashed|crushed|won against|won)\b/;
       const neutral   = /\b(versus|vs|against)\b/;
       let m, leftText, rightText, leftIsWinner = null;
       if ((m = namesText.match(loseVerbs)))      { [leftText, rightText] = namesText.split(m[0]); leftIsWinner = false; }
       else if ((m = namesText.match(winVerbs)))  { [leftText, rightText] = namesText.split(m[0]); leftIsWinner = true; }
       else if ((m = namesText.match(neutral)))   { [leftText, rightText] = namesText.split(m[0]); leftIsWinner = null; }
       else return { error: "Couldn't tell the two sides apart. Try e.g. \"Aditya beat Sohan 21-18\"." };
       const splitPlayers = t => (t || '').split(/\band\b|&|\bwith\b|,|\bplus\b/).map(s => s.trim()).filter(Boolean);
       const resolve = toks => toks.map(tok => { const r = nwMatchPlayerToken(tok); return { token: tok, player: r.player, ambiguous: !!r.ambiguous, options: r.options }; });
       const teamA = resolve(splitPlayers(leftText));
       const teamB = resolve(splitPlayers(rightText));
       let scoreA = null, scoreB = null;
       if (s1 != null && s2 != null) {
         const hi = Math.max(s1, s2), lo = Math.min(s1, s2);
         if (leftIsWinner === true)  { scoreA = hi; scoreB = lo; }
         else if (leftIsWinner === false) { scoreA = lo; scoreB = hi; }
         else { scoreA = s1; scoreB = s2; }
       }
       if (!teamA.length || !teamB.length) return { error: "Didn't catch both sides. Say who played on each side." };
       return { teamA, teamB, scoreA, scoreB, matchType: (teamA.length > 1 || teamB.length > 1) ? 'doubles' : 'singles' };
    }

    function nwApplyParsedToForm(p) {
       const mt = document.getElementById('match_type_select');
       mt.value = p.matchType;
       mt.dispatchEvent(new Event('change'));
       const set = (id, entry) => { const el = document.getElementById(id); if (el && entry && entry.player) el.value = entry.player.player_id; };
       set('team_a1_select', p.teamA[0]); set('team_a2_select', p.teamA[1]);
       set('team_b1_select', p.teamB[0]); set('team_b2_select', p.teamB[1]);
       const live = document.getElementById('live_scoring_toggle');
       if (p.scoreA != null && !(live && live.checked)) {
         document.getElementById('score_a').value = p.scoreA;
         document.getElementById('score_b').value = p.scoreB;
       }
    }

    function nwVoicePreviewHtml(p) {
       const side = arr => arr.map(e => {
         if (e.player) return `<b>${formatPlayerLabel(e.player.name, e.player.nickname)}</b>`;
         if (e.ambiguous && e.options) return `<span style="color:#c0392b;">${e.token}? (${e.options.map(o => o.name).join(' or ')})</span>`;
         return `<span style="color:#c0392b;">${e.token}?</span>`;
       }).join(' &amp; ');
       const anyUnmatched = [...p.teamA, ...p.teamB].some(e => !e.player);
       const score = p.scoreA != null ? ` &nbsp; <b>${p.scoreA}-${p.scoreB}</b>` : '';
       return `Heard: ${side(p.teamA)} vs ${side(p.teamB)}${score}` +
         (anyUnmatched ? `<div style="color:#c0392b;margin-top:4px;">Some names in red weren't matched - pick them manually before recording.</div>` : '');
    }

    function nwVoiceMatchInit() {
       const form = document.getElementById('match-form');
       if (!form || document.getElementById('nw-voice-wrap')) return;
       const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

       const wrap = document.createElement('div');
       wrap.id = 'nw-voice-wrap';
       wrap.style.cssText = 'margin:0 0 10px;';
       wrap.innerHTML =
         '<button type="button" id="nw-voice-btn" style="display:inline-flex;align-items:center;gap:8px;padding:9px 14px;border:0;border-radius:10px;background:var(--court,#2fa968);color:#fff;font-weight:600;cursor:pointer;">\uD83C\uDFA4 Record by voice</button>' +
         '<span id="nw-voice-hint" style="margin-left:10px;font-size:12px;color:var(--text-secondary,#888);">e.g. "Aditya and Sohan beat Sourabh and Mayank 21-18"</span>' +
         '<div id="nw-voice-status" style="margin-top:8px;font-size:13px;"></div>';
       form.parentNode.insertBefore(wrap, form);
       applyVoiceVisibility();

       const btn = wrap.querySelector('#nw-voice-btn');
       const status = wrap.querySelector('#nw-voice-status');

       if (!SR) {
         btn.disabled = true; btn.style.opacity = '0.5'; btn.style.cursor = 'not-allowed';
         wrap.querySelector('#nw-voice-hint').textContent = 'Voice input isn\'t supported in this browser - use Safari/Chrome, or fill the form normally.';
         return;
       }

       let listening = false;
       let recognizer = null;
       let fullTranscript = '';
       const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

       const stopListening = () => {
         if (recognizer) {
           listening = false;
           try { recognizer.stop(); } catch(e){}
         }
       };

       btn.addEventListener('click', () => {
         if (listening) {
           stopListening();
           return;
         }

         const rec = new SR();
         recognizer = rec;
         rec.lang = 'en-IN';
         rec.continuous = !isIOS;
         rec.interimResults = true;
         rec.maxAlternatives = 1;
         listening = true;
         fullTranscript = '';

         btn.textContent = '\u231B Warmup mic...';
         status.style.color = 'var(--text-secondary,#888)';
         status.textContent = 'Opening microphone... wait a moment before speaking.';

         rec.onstart = () => {
           btn.textContent = '\u23F9 Stop & fill';
           status.textContent = '\uD83D\uDD34 Listening - Speak now!';
         };

         rec.onresult = (ev) => {
           let currentSessionText = '';
           for (let i = 0; i < ev.results.length; i++) {
             currentSessionText += ev.results[i][0].transcript + ' ';
           }
           const combined = (fullTranscript + ' ' + currentSessionText).trim();
           status.textContent = '\u201C' + combined + '\u201D';
         };

         rec.onerror = (ev) => {
           if (ev.error === 'no-speech') return;
           listening = false;
           btn.textContent = '\uD83C\uDFA4 Record by voice';
           status.style.color = '#c0392b';
           status.textContent = ev.error === 'not-allowed'
             ? 'Microphone blocked - allow mic access for this site and try again.'
             : 'Voice error: ' + ev.error;
         };

         rec.onend = () => {
           const currentSaid = (status.textContent || '').replace(/^\u201C|\u201D$/g, '').trim();
           if (isIOS && listening && currentSaid) {
             fullTranscript = currentSaid + ' ';
             try { rec.start(); return; } catch(e){}
           }

           listening = false;
           recognizer = null;
           btn.textContent = '\uD83C\uDFA4 Record by voice';
           const said = currentSaid;
           if (!said || said.startsWith('Voice error') || said.startsWith('Microphone') || said.startsWith('Opening')) {
             if (!status.textContent.startsWith('Voice error') && !status.textContent.startsWith('Microphone')) {
               status.textContent = 'Didn\'t catch anything - tap record, wait for red indicator, then speak.';
             }
             return;
           }
           const parsed = nwParseMatchTranscript(said);
           if (parsed.error) {
             status.style.color = '#c0392b';
             status.innerHTML = '\u201C' + said + '\u201D<br>' + parsed.error;
             return;
           }
           nwApplyParsedToForm(parsed);
           status.style.color = 'var(--text,#111)';
           status.innerHTML = nwVoicePreviewHtml(parsed) +
             '<div style="margin-top:4px;color:var(--text-secondary,#888);">Filled the form - review and tap Record match.</div>';
         };

         try { rec.start(); } catch(e){ listening = false; btn.textContent = '\uD83C\uDFA4 Record by voice'; }
       });
    }
    nwVoiceMatchInit();
    // ================= end voice match entry =================

    // ================= Team pairing preview =================
    // Mirrors the tournament pairing (seeded = sort by Elo then snake-pair
    // strongest+weakest; random = shuffle) so you can preview balanced teams
    // from any selected players WITHOUT creating a tournament.
    function nwSeeded(p) { return [...p].sort((x, y) => (+y.rating || 1000) - (+x.rating || 1000)); }
    function nwShuffle(a) { a = [...a]; for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; }

    function nwPairingRefreshList() {
      const grp = document.getElementById('nw-pp-group');
      if (grp) { const cur = grp.value; populateSelect(grp, allGroups, 'group_id', 'group_name', '— choose —'); grp.value = cur; }
      const list = document.getElementById('nw-pp-list');
      if (!list) return;
      const sorted = [...allPlayers].sort((a, b) => String(a.name).localeCompare(String(b.name)));
      list.innerHTML = sorted.map(p =>
        `<label style="display:block;padding:2px 0;font-size:13px;"><input type="checkbox" class="nw-pp-cb" value="${p.player_id}"> ${escapeHtml(p.name)} <span style="color:var(--text-secondary,#888);">(${p.rating})</span></label>`
      ).join('');
      nwPairingUpdateCount();
    }
    function nwPairingUpdateCount() {
      const n = document.querySelectorAll('.nw-pp-cb:checked').length;
      const el = document.getElementById('nw-pp-count');
      if (el) el.textContent = n ? `${n} selected` : '';
    }
    function nwPairingRender() {
      const out = document.getElementById('nw-pp-out');
      const type = document.getElementById('nw-pp-type').value;
      const mode = document.getElementById('nw-pp-mode').value;
      const ids = [...document.querySelectorAll('.nw-pp-cb:checked')].map(c => c.value);
      const picked = ids.map(id => allPlayers.find(p => p.player_id === id)).filter(Boolean);
      if (type === 'doubles' && picked.length < 4) { out.innerHTML = '<span style="color:#c0392b;">Pick at least 4 players for doubles.</span>'; return; }
      if (type === 'singles' && picked.length < 2) { out.innerHTML = '<span style="color:#c0392b;">Pick at least 2 players.</span>'; return; }
      const ordered = mode === 'balanced' ? nwSeeded(picked) : nwShuffle(picked);
      let html = '';
      if (type === 'doubles') {
        let pairs = [], leftover = null;
        if (mode === 'balanced') { let i = 0, j = ordered.length - 1; while (i < j) { pairs.push([ordered[i], ordered[j]]); i++; j--; } if (i === j) leftover = ordered[i]; }
        else { const o = [...ordered]; if (o.length % 2) leftover = o.pop(); for (let i = 0; i < o.length; i += 2) pairs.push([o[i], o[i + 1]]); }
        html = '<table><tr><th>Team</th><th>Players</th><th>Combined Elo</th></tr>';
        pairs.forEach((pr, idx) => {
          const tot = (+pr[0].rating || 1000) + (+pr[1].rating || 1000);
          html += `<tr><td>${idx + 1}</td><td>${escapeHtml(pr[0].name)} (${pr[0].rating}) &amp; ${escapeHtml(pr[1].name)} (${pr[1].rating})</td><td>${tot}</td></tr>`;
        });
        html += '</table>';
        if (leftover) html += `<p style="font-size:12px;color:var(--text-secondary,#888);margin-top:6px;">Sitting out: <b>${escapeHtml(leftover.name)}</b> (${leftover.rating})</p>`;
        if (pairs.length > 1) {
          const totals = pairs.map(p => (+p[0].rating || 1000) + (+p[1].rating || 1000));
          html += `<p style="font-size:12px;color:var(--text-secondary,#888);">Team-total spread: ${Math.max(...totals) - Math.min(...totals)} (lower = more balanced)</p>`;
        }
      } else {
        html = '<table><tr><th>Seed</th><th>Player</th><th>Elo</th></tr>';
        ordered.forEach((p, i) => { html += `<tr><td>${i + 1}</td><td>${escapeHtml(p.name)}</td><td>${p.rating}</td></tr>`; });
        html += '</table>';
      }
      out.innerHTML = html;
    }
    function nwPairingInit() {
      const anchor = document.getElementById('record-match-card');
      if (!anchor || document.getElementById('nw-pairing-card')) return;
      const card = document.createElement('div');
      card.className = 'card';
      card.id = 'nw-pairing-card';
      card.innerHTML =
        '<h2>Team pairing preview</h2>' +
        '<p style="font-size:12px;color:var(--text-secondary,#888);">Pick players and preview balanced or random teams by current Elo — no tournament needed.</p>' +
        '<div class="row">' +
          '<div><label>Quick-pick group<select id="nw-pp-group"><option value="">— choose —</option></select></label></div>' +
          '<div><label>Type<select id="nw-pp-type"><option value="doubles">Doubles</option><option value="singles">Singles</option></select></label></div>' +
          '<div><label>Pairing<select id="nw-pp-mode"><option value="balanced">Balanced (by Elo)</option><option value="random">Random</option></select></label></div>' +
        '</div>' +
        '<div style="margin:8px 0;">' +
          '<button type="button" id="nw-pp-all" class="secondary" style="padding:4px 10px;font-size:12px;margin:0;">Select all</button> ' +
          '<button type="button" id="nw-pp-clear" class="secondary" style="padding:4px 10px;font-size:12px;margin:0;">Clear</button>' +
          '<span id="nw-pp-count" style="font-size:12px;color:var(--text-secondary,#888);margin-left:8px;"></span>' +
        '</div>' +
        '<div id="nw-pp-list" style="max-height:200px;overflow-y:auto;border:1px solid var(--line,#333);border-radius:8px;padding:8px;"></div>' +
        '<button type="button" id="nw-pp-go" style="margin-top:10px;">Preview teams</button>' +
        '<div id="nw-pp-out" style="margin-top:12px;"></div>';
      anchor.parentNode.insertBefore(card, anchor.nextSibling);

      populateSelect(document.getElementById('nw-pp-group'), allGroups, 'group_id', 'group_name', '— choose —');
      nwPairingRefreshList();

      document.getElementById('nw-pp-all').addEventListener('click', () => { document.querySelectorAll('.nw-pp-cb').forEach(c => c.checked = true); nwPairingUpdateCount(); });
      document.getElementById('nw-pp-clear').addEventListener('click', () => { document.querySelectorAll('.nw-pp-cb').forEach(c => c.checked = false); nwPairingUpdateCount(); });
      document.getElementById('nw-pp-list').addEventListener('change', nwPairingUpdateCount);
      document.getElementById('nw-pp-go').addEventListener('click', nwPairingRender);
      document.getElementById('nw-pp-group').addEventListener('change', (e) => {
        const g = allGroups.find(x => x.group_id === e.target.value);
        const ids = new Set((g && g.member_ids) || []);
        document.querySelectorAll('.nw-pp-cb').forEach(c => c.checked = ids.has(c.value));
        nwPairingUpdateCount();
      });
    }
    nwPairingInit();
    // ================= end team pairing preview =================

    document.getElementById('match-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const group_id = document.getElementById('match_group_select').value || null;
      const match_type = document.getElementById('match_type_select').value;
      // A group is required for everyone except a SuperAdmin, who may
      // deliberately record an ungrouped one-off. This keeps new matches
      // attributed without removing the escape hatch entirely.
      if (!group_id && !isSuperAdmin()) {
        document.getElementById('match-result').textContent = 'Please select a group for this match.';
        return;
      }
      const a1 = document.getElementById('team_a1_select').value;
      const a2 = document.getElementById('team_a2_select').value;
      const b1 = document.getElementById('team_b1_select').value;
      const b2 = document.getElementById('team_b2_select').value;
      const score_a = document.getElementById('score_a').value;
      const score_b = document.getElementById('score_b').value;
      const resultEl = document.getElementById('match-result');

      const team_a = match_type === 'doubles' ? [a1, a2] : [a1];
      const team_b = match_type === 'doubles' ? [b1, b2] : [b1];

      if (team_a.some(id => !id) || team_b.some(id => !id)) {
        resultEl.textContent = 'Select all required players.';
        return;
      }
      if (new Set([...team_a, ...team_b]).size !== team_a.length + team_b.length) {
        resultEl.textContent = 'A player cannot appear on both teams or twice.';
        return;
      }

      const nameFor = (pid) => { const p = allPlayers.find(pl => pl.player_id === pid); return p ? p.name : pid; };
      const teamAName = team_a.map(nameFor).join(' & ');
      const teamBName = team_b.map(nameFor).join(' & ');
      const scoreDisplay = document.getElementById('live_scoring_toggle').checked
        ? '(final score determined by live scoring)'
        : `${score_a}-${score_b}`;
      if (!await nwConfirm(`Confirm recording this match?\n\n${teamAName} vs ${teamBName}\nScore: ${scoreDisplay}`)) {
        return;
      }

      resultEl.textContent = 'Recording...';
      try {
        const useLive = document.getElementById('live_scoring_toggle').checked;
        const points_to_win = document.getElementById('points_to_win_select').value;
        const payload = { group_id, match_type, team_a, team_b, score_a, score_b, points_to_win };
        if (useLive) {
          if (livePointLog.length === 0) {
            resultEl.textContent = 'Record at least one point before finishing.';
            return;
          }
          payload.point_log = livePointLog;
        }

        const { res, data, error, sessionExpired } = await authedFetch(`${API_BASE_URL}/record-match`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          clearPendingMatch();
          let msg = `Match recorded: ${data.team_a_names.join(' & ')} vs ${data.team_b_names.join(' & ')} (${data.score_a}-${data.score_b})`;
          if (data.momentum && data.momentum.winner_overcame_deficit > 0) {
            msg += ` - came back from a ${data.momentum.winner_overcame_deficit}-point deficit!`;
          }
          showMatchOutcome(true, msg);
          document.getElementById('score_a').value = '';
          document.getElementById('score_b').value = '';
          livePointLog = [];
          if (useLive) updateLiveScoreDisplay();
          loadPlayers();
          // If the Player Card happens to be showing someone who just
          // played, its numbers are now stale - refresh it in place
          // rather than making you switch players and switch back.
          refreshProfileIfShowing([...team_a, ...team_b]);
        } else {
          // Never lose the work: stash it so it can be resubmitted, then
          // either prompt re-login (unrecoverable session) or show the
          // error with a note that it was saved.
          savePendingMatch(payload, { teamA: teamAName, teamB: teamBName,
            score: useLive ? '(live score)' : `${score_a}-${score_b}` });
          offerPendingMatchRestore();
          if (sessionExpired) {
            handleSessionExpired();
          } else {
            showMatchOutcome(false, `${error} - your match was saved; use "Record it now" to retry.`);
          }
        }
      } catch (err) {
        showMatchOutcome(false, `Request failed: ${err.message} - check your connection and try again.`);
      }
    });

    /**
     * A failed match submission used to be a plain grey sentence in an
     * element that is often scrolled off-screen on a phone, which is how
     * a match got lost during a live game without anyone noticing. Now a
     * failure is coloured, scrolled into view, AND blocks on an alert -
     * you cannot walk away from a failed save thinking it saved.
     */
    function showMatchOutcome(ok, message) {
      const el = document.getElementById('match-result');
      el.textContent = message;
      el.style.padding = '10px 12px';
      el.style.borderRadius = 'var(--radius)';
      el.style.fontWeight = '600';
      el.style.borderLeft = `4px solid ${ok ? 'var(--court)' : 'var(--smash)'}`;
      el.style.background = ok ? 'rgba(47,169,104,0.12)' : 'rgba(214,64,64,0.12)';
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      if (!ok) nwAlert(`Match NOT recorded\n\n${message}`);
    }

    // ---------- unsaved-match safety net ----------
    // A live match takes real effort to enter (the point log especially).
    // If the save fails - a dead session on a court phone, or venue wifi
    // dropping mid-submit - we must never make someone re-enter it by hand.
    // So a failed save is stashed locally and offered back for a one-tap
    // resubmit, and an unrecoverable session pops an explicit re-login.
    const PENDING_MATCH_KEY = 'nw_pending_match';
    function savePendingMatch(payload, meta) {
      try { localStorage.setItem(PENDING_MATCH_KEY, JSON.stringify({ payload, meta, savedAt: Date.now() })); }
      catch (_) { /* storage blocked/full - best effort, nothing else to do */ }
    }
    function loadPendingMatch() {
      try { return JSON.parse(localStorage.getItem(PENDING_MATCH_KEY) || 'null'); } catch (_) { return null; }
    }
    function clearPendingMatch() {
      try { localStorage.removeItem(PENDING_MATCH_KEY); } catch (_) {}
    }

    // AWS-console-style "your session expired, log in again" prompt, shown
    // only when the token genuinely can't be refreshed. Reassures that the
    // match is saved so nobody panics and re-enters it.
    function handleSessionExpired() {
      if (document.getElementById('session-expired-modal')) return;
      const overlay = document.createElement('div');
      overlay.id = 'session-expired-modal';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
      overlay.innerHTML =
        '<div style="background:var(--card,#fff);color:var(--text,#111);max-width:380px;width:100%;padding:22px;border-radius:14px;box-shadow:0 12px 44px rgba(0,0,0,0.35);">' +
          '<h3 style="margin:0 0 8px;font-size:18px;">Session expired</h3>' +
          '<p style="margin:0 0 16px;line-height:1.5;font-size:14px;">You\'ve been signed out. Log in again to continue \u2014 your match has been saved and will be waiting for you when you\'re back.</p>' +
          '<button id="session-expired-login" style="width:100%;padding:11px;border:0;border-radius:10px;background:var(--court,#2fa968);color:#fff;font-weight:700;font-size:15px;cursor:pointer;">Log in again</button>' +
        '</div>';
      document.body.appendChild(overlay);
      document.getElementById('session-expired-login').addEventListener('click', () => {
        overlay.remove();
        authSession = null;
        try { const u = userPool && userPool.getCurrentUser(); if (u) u.signOut(); } catch (_) {}
        updateAuthUI();
        openAuthModal();
        showAuthView('login');
      });
    }

    // Banner above the match form offering to resubmit a stashed match.
    function ensureRestoreHost() {
      const anchor = document.getElementById('match-result');
      if (anchor && !document.getElementById('match-restore-host')) {
        const host = document.createElement('div');
        host.id = 'match-restore-host';
        anchor.parentNode.insertBefore(host, anchor);
      }
      return document.getElementById('match-restore-host');
    }
    function offerPendingMatchRestore() {
      const host = ensureRestoreHost();
      if (!host) return;
      const pend = loadPendingMatch();
      if (!pend) { host.innerHTML = ''; return; }
      const when = new Date(pend.savedAt).toLocaleString();
      const m = pend.meta || {};
      host.innerHTML =
        '<div style="border-left:4px solid var(--court,#2fa968);background:rgba(47,169,104,0.10);padding:10px 12px;border-radius:10px;margin:0 0 10px;font-size:13px;">' +
          '<strong>Unsaved match found</strong> (' + (m.teamA || '?') + ' vs ' + (m.teamB || '?') + ', ' + (m.score || '') + ') from ' + when + '.' +
          '<div style="margin-top:8px;display:flex;gap:8px;">' +
            '<button id="pending-record" style="padding:7px 12px;border:0;border-radius:8px;background:var(--court,#2fa968);color:#fff;font-weight:600;cursor:pointer;">Record it now</button>' +
            '<button id="pending-discard" style="padding:7px 12px;border-radius:8px;background:transparent;color:var(--text,#111);border:1px solid var(--line,#ccc);cursor:pointer;">Discard</button>' +
          '</div>' +
        '</div>';
      document.getElementById('pending-discard').onclick = () => { clearPendingMatch(); offerPendingMatchRestore(); };
      document.getElementById('pending-record').onclick = async () => {
        const btn = document.getElementById('pending-record');
        btn.disabled = true; btn.textContent = 'Recording...';
        try {
          const { res, error, sessionExpired } = await authedFetch(`${API_BASE_URL}/record-match`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(pend.payload)
          });
          if (res && res.ok) { clearPendingMatch(); offerPendingMatchRestore(); loadPlayers(); showMatchOutcome(true, 'Saved match recorded.'); }
          else if (sessionExpired) { handleSessionExpired(); btn.disabled = false; btn.textContent = 'Record it now'; }
          else { btn.disabled = false; btn.textContent = 'Record it now'; nwAlert('Still could not record: ' + (error || 'unknown')); }
        } catch (err) {
          btn.disabled = false; btn.textContent = 'Record it now'; nwAlert('Still could not record: ' + err.message);
        }
      };
    }

    // ---------- game log ----------

    async function loadGameLog() {
      const groupId = document.getElementById('log_group_filter').value;
      const playerId = document.getElementById('log_player_filter').value;
      const dateFrom = document.getElementById('log_date_from').value;
      const dateTo = document.getElementById('log_date_to').value;
      const logEl = document.getElementById('game-log');
      logEl.textContent = 'Loading...';

      const params = new URLSearchParams();
      if (groupId) params.set('group_id', groupId);
      if (playerId) params.set('player_id', playerId);
      if (dateFrom) params.set('date_from', dateFrom);
      if (dateTo) params.set('date_to', dateTo);

      try {
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { logEl.textContent = `Error: ${data.error}`; return; }
        // Opponent filter is applied client-side (the API returns team_a /
        // team_b as id arrays). If a player is also chosen, keep only matches
        // where the two were on OPPOSITE sides; otherwise any match the
        // opponent played in.
        const opponentId = (document.getElementById('log_opponent_filter') || {}).value || '';
        if (opponentId) {
          data.matches = data.matches.filter(m => {
            const a = m.team_a || [], b = m.team_b || [];
            const oppIn = a.includes(opponentId) || b.includes(opponentId);
            if (!playerId) return oppIn;
            const meA = a.includes(playerId), meB = b.includes(playerId);
            return (meA && b.includes(opponentId)) || (meB && a.includes(opponentId));
          });
        }
        if (!data.matches.length) { logEl.innerHTML = '<p style="font-size:13px;color:#555;">No matches yet.</p>'; return; }

        // Hand off to the paginated renderer. Large clubs accumulate hundreds
        // of matches; rendering them all at once is slow and unwieldy on a
        // phone, so the log is chunked into pages (filtering still happens on
        // the full set above - only the display is paged).
        gameLogRows = data.matches;
        gameLogPage = 0;
        renderGameLog();
      } catch (err) {
        logEl.textContent = `Request failed: ${err.message}`;
      }
    }

    let gameLogRows = [];
    let gameLogPage = 0;
    const GAME_LOG_PAGE_SIZE = 25;

    function gameLogGoto(p) { gameLogPage = p; renderGameLog(); }

    function renderGameLog() {
      const logEl = document.getElementById('game-log');
      const total = gameLogRows.length;
      const pages = Math.max(1, Math.ceil(total / GAME_LOG_PAGE_SIZE));
      if (gameLogPage >= pages) gameLogPage = pages - 1;
      if (gameLogPage < 0) gameLogPage = 0;
      const start = gameLogPage * GAME_LOG_PAGE_SIZE;
      const pageRows = gameLogRows.slice(start, start + GAME_LOG_PAGE_SIZE);

      let html = '<table><tr><th>Date</th><th>Team A</th><th>Team B</th><th>Score</th><th>Notes</th><th></th></tr>';
      pageRows.forEach(m => {
        const date = new Date(m.date).toLocaleString();
        const teamA = (m.team_a_names || []).join(' & ');
        const teamB = (m.team_b_names || []).join(' & ');
        let notes = '';
        if (m.momentum && m.momentum.winner_overcame_deficit > 0) {
          notes = `Comeback: overcame a ${m.momentum.winner_overcame_deficit}-point deficit`;
        }
        const perm = matchPermissions(m);
        const label = `${teamA} vs ${teamB}`;
        let actions = '';
        if (perm.canSee) {
          const editLabel = perm.canActDirectly ? 'Edit' : 'Request edit';
          const delLabel = perm.canActDirectly ? 'Delete' : 'Request delete';
          const gid = m.group_id || '';
          actions = `<button class="secondary" style="margin-top:0;padding:4px 8px;font-size:11px;" onclick="editMatch('${m.match_id}', '${gid}')">${editLabel}</button> `
                  + `<button class="secondary" style="margin-top:0;padding:4px 8px;font-size:11px;" onclick="deleteMatch('${m.match_id}', '${encodeURIComponent(label)}', '${gid}')">${delLabel}</button>`;
        }
        html += `<tr><td>${date}</td><td>${teamA}</td><td>${teamB}</td><td>${m.score_a} - ${m.score_b}</td><td>${notes}</td><td>${actions}</td></tr>`;
      });
      html += '</table>';

      if (pages > 1) {
        const from = start + 1, to = Math.min(start + GAME_LOG_PAGE_SIZE, total);
        html += `<div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-size:13px;">`
              + `<button class="secondary" style="margin:0;padding:5px 12px;" ${gameLogPage === 0 ? 'disabled' : ''} onclick="gameLogGoto(${gameLogPage - 1})">Prev</button>`
              + `<span style="color:var(--text-secondary);">${from}-${to} of ${total} &middot; page ${gameLogPage + 1}/${pages}</span>`
              + `<button class="secondary" style="margin:0;padding:5px 12px;" ${gameLogPage >= pages - 1 ? 'disabled' : ''} onclick="gameLogGoto(${gameLogPage + 1})">Next</button>`
              + `</div>`;
      }
      logEl.innerHTML = html;
    }
    document.getElementById('load-log-btn').addEventListener('click', loadGameLog);
    ['log_group_filter', 'log_player_filter', 'log_opponent_filter', 'log_date_from', 'log_date_to'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', loadGameLog);
    });

    /**
     * Who may see Edit/Delete on a match. Precedence, highest first:
     *   SuperAdmin      -> everything
     *   group owner     -> matches in their group
     *   group member    -> matches in their group
     *   match player    -> their own matches
     * Returns { canSee, canActDirectly }. Only SuperAdmin acts directly
     * (they're the approver); everyone else files a request.
     */
    function matchPermissions(m) {
      if (isSuperAdmin()) return { canSee: true, canActDirectly: true };
      const me = myPlayerId();
      if (!me) return { canSee: false, canActDirectly: false };
      // participant?
      const inMatch = [...(m.team_a || []), ...(m.team_b || [])].includes(me);
      // member (any role) of the match's group?
      let inGroup = false;
      if (m.group_id) {
        const g = allGroups.find(x => x.group_id === m.group_id);
        if (g && (g.member_ids || []).includes(me)) inGroup = true;
      }
      // An ungrouped match belongs to no one in particular, so any linked
      // player may request a change to it - there's no group boundary to
      // respect. Grouped matches stay restricted to their members and
      // participants. Either way, only SuperAdmin acts directly; everyone
      // else files a request.
      const ungrouped = !m.group_id;
      return { canSee: inMatch || inGroup || ungrouped, canActDirectly: false };
    }

    function matchGroupLabel(m) {
      const g = m.group_id && allGroups.find(x => x.group_id === m.group_id);
      return g ? g.group_name : '';
    }

    async function requestMatchChange(matchId, type, label, groupId, extra) {
      const reason = await nwPrompt(type === 'match_delete'
        ? `Request deletion of "${label}"?\n\nGive a reason for the admin:`
        : `Request a score correction for "${label}"?\n\nGive a reason for the admin:`);
      if (reason === null) return;
      if (!reason.trim()) { nwAlert('A reason is required.'); return; }
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/action-request`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type, match_id: matchId, match_label: label,
                                 group_id: groupId || null, reason: reason.trim(), ...(extra || {}) })
        });
        nwAlert(res.ok ? 'Request sent to the admin for approval.' : `Error: ${error}`);
      } catch (e) { nwAlert(`Request failed: ${e.message}`); }
    }

    // Full match edit (players + score), SuperAdmin only. Non-admins keep the
    // score-only request flow. Changing players recomputes every rating, same
    // as a score edit, since Elo is path-dependent.
    async function editMatch(matchId, groupId) {
      const m = (gameLogRows || []).find(r => r.match_id === matchId);
      if (!m) { return editMatchScore(matchId, 0, 0, '', groupId || ''); }
      if (!isSuperAdmin()) {
        // non-admins: fall back to the existing score-only request path
        const label = playerLabelsById(m.team_a, m.team_a_names).join(' & ') + ' vs ' + playerLabelsById(m.team_b, m.team_b_names).join(' & ');
        return editMatchScore(matchId, m.score_a, m.score_b, encodeURIComponent(label), groupId || '');
      }
      const size = (m.team_a || []).length || 1;
      const opts = (sel) => (allPlayers || []).map(p =>
        `<option value="${p.player_id}"${p.player_id === sel ? ' selected' : ''}>${escapeHtml(p.name)} (${escapeHtml(p.nickname)}) (${p.rating})</option>`).join('');
      const pickers = (team, prefix) => Array.from({ length: size }, (_, i) =>
        `<select class="nw-modal-input ${prefix}" style="margin-bottom:6px;">${opts((team || [])[i])}</select>`).join('');

      const overlay = document.createElement('div');
      overlay.className = 'nw-modal-overlay';
      overlay.innerHTML = `
        <div class="nw-modal" style="max-width:460px;">
          <div class="nw-modal-msg">Edit match \u2014 players &amp; score.<br><span style="font-size:12px;opacity:0.7;">Saving recomputes every player's rating from the corrected history.</span></div>
          <div style="display:flex; gap:12px;">
            <div style="flex:1;"><strong style="font-size:13px;">Team A</strong>${pickers(m.team_a, 'nw-ta')}
              <input type="number" class="nw-modal-input nw-sa" value="${m.score_a}" style="margin-top:4px;" placeholder="Score A"></div>
            <div style="flex:1;"><strong style="font-size:13px;">Team B</strong>${pickers(m.team_b, 'nw-tb')}
              <input type="number" class="nw-modal-input nw-sb" value="${m.score_b}" style="margin-top:4px;" placeholder="Score B"></div>
          </div>
          <input type="text" class="nw-modal-input nw-code" placeholder="Confirmation code" style="margin-top:10px;">
          <div class="nw-modal-actions">
            <button class="nw-modal-btn nw-cancel">Cancel</button>
            <button class="nw-modal-btn nw-primary nw-save">Save &amp; recompute</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      requestAnimationFrame(() => overlay.classList.add('nw-open'));
      const close = () => { overlay.classList.remove('nw-open'); setTimeout(() => overlay.remove(), 140); };
      overlay.querySelector('.nw-cancel').onclick = close;
      overlay.addEventListener('mousedown', e => { if (e.target === overlay) close(); });
      overlay.querySelector('.nw-save').onclick = async () => {
        const team_a = [...overlay.querySelectorAll('.nw-ta')].map(s => s.value);
        const team_b = [...overlay.querySelectorAll('.nw-tb')].map(s => s.value);
        const score_a = parseInt(overlay.querySelector('.nw-sa').value, 10);
        const score_b = parseInt(overlay.querySelector('.nw-sb').value, 10);
        const confirm = overlay.querySelector('.nw-code').value;
        if (new Set([...team_a, ...team_b]).size !== team_a.length + team_b.length) { nwAlert('A player can\'t be on both teams (or picked twice).'); return; }
        if (isNaN(score_a) || isNaN(score_b) || score_a === score_b) { nwAlert('Enter two different scores.'); return; }
        if (!confirm) { nwAlert('Enter the confirmation code.'); return; }
        try {
          const res = await fetch(`${API_BASE_URL}/matches/${matchId}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ team_a, team_b, score_a, score_b, confirm })
          });
          const data = await res.json();
          if (!res.ok) { nwAlert('Error: ' + (data.error || 'could not save')); return; }
          close();
          nwAlert('Match updated. All ratings were recomputed.');
          loadGameLog(); loadPlayers();
        } catch (e) { nwAlert('Request failed: ' + e.message); }
      };
    }

    async function editMatchScore(matchId, currentScoreA, currentScoreB, encLabel, groupId) {
      const label = encLabel ? decodeURIComponent(encLabel) : matchId;
      const newScoreA = await nwPrompt(`Current score: ${currentScoreA} - ${currentScoreB}\nEnter corrected Team A score:`, currentScoreA);
      if (newScoreA === null) return;
      const newScoreB = await nwPrompt('Enter corrected Team B score:', currentScoreB);
      if (newScoreB === null) return;

      // Non-admins file a request with a reason; only SuperAdmin edits live.
      if (!isSuperAdmin()) {
        return requestMatchChange(matchId, 'match_edit', label, groupId,
          { new_score_a: parseInt(newScoreA, 10), new_score_b: parseInt(newScoreB, 10) });
      }

      const confirmText = await nwPrompt('Enter the confirmation code to save this correction. Note: this will recompute every player\'s rating from the corrected history.');
      if (!confirmText) return;

      try {
        const res = await fetch(`${API_BASE_URL}/matches/${matchId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ score_a: parseInt(newScoreA, 10), score_b: parseInt(newScoreB, 10), confirm: confirmText })
        });
        const data = await res.json();
        if (res.ok) {
          nwAlert('Score corrected. All ratings have been recomputed.');
          loadGameLog();
          loadPlayers();
        } else {
          nwAlert(`Error: ${data.error}`);
        }
      } catch (err) {
        nwAlert(`Request failed: ${err.message}`);
      }
    }

    async function deleteMatch(matchId, encLabel, groupId) {
      const label = encLabel ? decodeURIComponent(encLabel) : matchId;
      // Non-admins file a request with a reason; only SuperAdmin deletes live.
      if (!isSuperAdmin()) {
        return requestMatchChange(matchId, 'match_delete', label, groupId);
      }

      if (!await nwConfirm('Permanently delete this match? This cannot be undone, and every player\'s rating will be recomputed from the remaining history.')) return;

      const confirmText = await nwPrompt('Enter the confirmation code to permanently delete this match.');
      if (!confirmText) return;

      try {
        const res = await fetch(`${API_BASE_URL}/matches/${matchId}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: confirmText })
        });
        const data = await res.json();
        if (res.ok) {
          nwAlert('Match deleted. All ratings have been recomputed.');
          loadGameLog();
          loadPlayers();
        } else {
          nwAlert(`Error: ${data.error}`);
        }
      } catch (err) {
        nwAlert(`Request failed: ${err.message}`);
      }
    }

    function downloadCSV(filename, rows) {
      const csv = rows.map(row => row.map(cell => `"${String(cell ?? '').replace(/"/g, '""')}"`).join(',')).join('\n');
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }

    document.getElementById('export-players-csv-btn').addEventListener('click', () => {
      const rows = [['Player ID', 'Name', 'Skill Level', 'Rating']];
      allPlayers.forEach(p => rows.push([p.player_id, p.name, p.skill_level, p.rating]));
      downloadCSV('networth-players.csv', rows);
    });

    document.getElementById('export-matches-csv-btn').addEventListener('click', async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/matches`);
        const data = await res.json();
        const rows = [['Date', 'Match Type', 'Team A', 'Team B', 'Score A', 'Score B', 'Winner', 'Group ID', 'Tournament ID', 'Stage']];
        (data.matches || []).forEach(m => rows.push([
          m.date, m.match_type, (m.team_a_names || []).join(' & '), (m.team_b_names || []).join(' & '),
          m.score_a, m.score_b, m.winner, m.group_id || '', m.tournament_id || '', m.stage || ''
        ]));
        downloadCSV('networth-matches.csv', rows);
      } catch (err) {
        nwAlert(`Export failed: ${err.message}`);
      }
    });

    async function loadRankings() {
      const scope = document.getElementById('rankings_scope_select').value;
      const resultEl = document.getElementById('rankings-result');
      resultEl.textContent = 'Loading...';

      try {
        let rankedPlayers;
        if (scope) {
          const res = await fetch(`${API_BASE_URL}/groups/${scope}`);
          const data = await res.json();
          if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
          rankedPlayers = data.members;
        } else {
          rankedPlayers = allPlayers;
        }

        if (!rankedPlayers.length) { resultEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No players to rank.</p>'; return; }

        // Only players with enough games are ranked - a rating from 0-4 games
        // is mostly noise (one lucky win can outrank someone who earned their
        // spot over dozens of games). Provisional players are listed below,
        // unranked, so they can see how many more games until they count.
        const MIN_GAMES = 5;
        const gp = (p) => Number(p.games_played || 0);
        const eligible = rankedPlayers.filter(p => gp(p) >= MIN_GAMES);
        const provisional = rankedPlayers.filter(p => gp(p) > 0 && gp(p) < MIN_GAMES);

        const sorted = [...eligible].sort((a, b) => Number(b.rating) - Number(a.rating));
        // Rank each player a second time by their previous rating, so we can
        // show whether they climbed or fell after their most recent match.
        // Green up-arrow = moved up, red down = fell, dash = unchanged/new.
        const prevSorted = [...eligible].sort((a, b) =>
          Number(b.previous_rating ?? b.rating) - Number(a.previous_rating ?? a.rating));
        const prevRankById = {};
        prevSorted.forEach((p, i) => { prevRankById[p.player_id] = i; });

        let html = '<table><tr><th>#</th><th></th><th>Player</th><th>Rating</th></tr>';
        sorted.forEach((p, idx) => {
          const label = formatPlayerLabel(p.name, p.nickname);
          const prevRank = prevRankById[p.player_id];
          let arrow = '<span style="color:var(--text-secondary);">–</span>';
          if (prevRank !== undefined) {
            const moved = prevRank - idx;  // positive = climbed
            if (moved > 0) arrow = `<span style="color:#2FA968;" title="Up ${moved}">▲</span>`;
            else if (moved < 0) arrow = `<span style="color:#d6403f;" title="Down ${-moved}">▼</span>`;
          }
          html += `<tr><td>${idx + 1}</td><td>${arrow}</td><td>${label}</td><td class="rating">${p.rating}</td></tr>`;
        });
        html += '</table>';
        if (!sorted.length) {
          html = `<p style="font-size:13px;color:var(--text-secondary);">No one has played ${MIN_GAMES}+ games yet, so no one is ranked.</p>`;
        }
        if (provisional.length) {
          html += `<p style="font-size:12px;color:var(--text-secondary);margin-top:14px;">Provisional \u2014 not yet ranked (need ${MIN_GAMES} games):</p><table>`;
          provisional.sort((a, b) => gp(b) - gp(a)).forEach(p => {
            html += `<tr><td>${formatPlayerLabel(p.name, p.nickname)}</td>`
                  + `<td style="color:var(--text-secondary);font-size:12px;">${gp(p)}/${MIN_GAMES} games</td></tr>`;
          });
          html += '</table>';
        }
        resultEl.innerHTML = html;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }
    document.getElementById('load-rankings-btn').addEventListener('click', loadRankings);
    document.getElementById('rankings_scope_select').addEventListener('change', loadRankings);

    async function fetchRatingHistory(playerId) {
      const res = await fetch(`${API_BASE_URL}/profile-secure/matches?player_id=${playerId}`, { headers: getAuthHeaders() });
      const data = await res.json();
      const matches = (data.matches || []).slice().sort((a, b) => a.date.localeCompare(b.date));
      return matches
        .filter(m => m.ratings_after && m.ratings_after[playerId] !== undefined)
        .map(m => ({ x: m.date, y: Number(m.ratings_after[playerId]) }));
    }

    let profileRatingChart = null;

    async function loadVisiblePlayers(opts = {}) {
      if (!isLoggedIn()) return;
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/visible-players`);
        if (!res.ok) return;
        const visible = data.players || [];
        // Keep the shared roster cache in step with what just came back.
        // Without this, /visible-players data (fresh ratings, avatar and
        // banner ids) never reached allPlayers, which is the cache the
        // Player Card banner and the Settings pickers actually read from
        // - so a cosmetic change saved fine server-side and then appeared
        // not to have happened.
        visible.forEach(vp => {
          const idx = allPlayers.findIndex(p => p.player_id === vp.player_id);
          if (idx >= 0) allPlayers[idx] = { ...allPlayers[idx], ...vp };
        });
        const labeledVisible = visible.map(p => ({
          ...p, label: `${formatPlayerLabel(p.name, p.nickname)} (${p.rating})`
        }));
        // Note: the "added by <email>" audit tag used to be appended here
        // for SuperAdmins, but this is the Player Card picker - a viewing
        // control, not an admin one - so the extra text just cluttered
        // every option. The audit trail still lives on the record and in
        // the request queue; it doesn't belong in this dropdown.
        // Capture whatever's currently selected BEFORE repopulating (e.g.
        // from a display-mode toggle re-render) so re-populating the
        // options doesn't silently yank the view back to your own card
        // if you were deliberately looking at someone else's.
        const previousSelection = document.getElementById('profile_player_select').value;
        // ...but only if it was made by THIS account. The DOM survives a
        // logout/login in the same tab, so without the ownership check a
        // second person logging in inherits the first person's selected
        // card and never sees their own by default.
        const selectionIsMine = profileSelectionOwner === myPlayerId();
        populateSelect(document.getElementById('profile_player_select'), labeledVisible, 'player_id', 'label', null);
        populateSelect(document.getElementById('profile_h2h_opponent_select'), labeledVisible, 'player_id', 'label', null);
        populateSelect(document.getElementById('profile_partner_select'), labeledVisible, 'player_id', 'label', null);
        populateSelect(document.getElementById('profile_compare2_select'), labeledVisible, 'player_id', 'label', 'None');
        populateSelect(document.getElementById('profile_compare3_select'), labeledVisible, 'player_id', 'label', 'None');
        populateSelect(document.getElementById('profile_compare4_select'), labeledVisible, 'player_id', 'label', 'None');
        const myId = myPlayerId();
        const isFreshLoad = !previousSelection || !selectionIsMine;
        if (previousSelection && selectionIsMine && visible.some(p => p.player_id === previousSelection)) {
          // Keep whoever was already selected (this is a re-render, e.g.
          // from the display toggle, not the first load) - and since
          // nothing about WHO is selected changed, there's no need to
          // re-fetch their profile data either.
          document.getElementById('profile_player_select').value = previousSelection;
        } else if (myId && visible.some(p => p.player_id === myId)) {
          // First load with no prior selection - default to your own card,
          // not just whatever's alphabetically first.
          document.getElementById('profile_player_select').value = myId;
        }
        profileSelectionOwner = myId;
        // Only fetch profile data on a genuine first population - a
        // toggle-triggered relabel of the SAME already-selected player
        // must cost zero new API calls, matching the whole point of
        // this toggle existing.
        // opts.keepSelection means refreshProfile() is driving this and
        // will call loadProfile() itself straight after - firing it here
        // too would double every request on a manual refresh.
        if (isFreshLoad && visible.length && !opts.keepSelection) loadProfile();
      } catch (err) { /* silent - profile tab just stays empty until this succeeds */ }
    }

    // ---------- profile card customization (avatar + banner presets) ----------
    // Curated presets, not uploaded images - no upload infrastructure yet.
    // Real photo upload would be a bigger separate step (needs S3
    // presigned uploads); this delivers visible personalization now with
    // zero new infra, and could sit alongside real uploads later.
    const AVATAR_PRESETS = {
      shuttle: '🏸', trophy: '🏆', lightning: '⚡', fire: '🔥', target: '🎯',
      eagle: '🦅', tiger: '🐯', lion: '🦁', wolf: '🐺', fox: '🦊',
      dragon: '🐉', crown: '👑', muscle: '💪', star: '🌟', game: '🎮', racket: '🏓'
    };
    /**
     * Banners are patterns, not flat gradients - a plain two-stop gradient
     * is the thing every template ships with, and at 110px tall it reads
     * as a coloured bar rather than as anyone's choice. Each preset layers
     * a repeating pattern over a base gradient, using per-layer
     * `position / size` syntax inside one shorthand. Pure CSS: no image
     * assets, so nothing new to host, cache, or pay egress on.
     */
    const BANNER_PRESETS = {
      // Court lines - the obvious one to have, and the only preset drawn
      // from the actual subject rather than from generic pattern stock.
      court: `repeating-linear-gradient(90deg, rgba(255,255,255,.16) 0 2px, transparent 2px 88px),
              repeating-linear-gradient(0deg, rgba(255,255,255,.10) 0 2px, transparent 2px 54px),
              linear-gradient(120deg, #0b3018 0%, #1F7A4D 55%, #2FA968 100%)`,
      // Speed streaks, angled the way a smash travels.
      smash: `repeating-linear-gradient(115deg, rgba(255,255,255,.10) 0 4px, transparent 4px 26px),
              linear-gradient(120deg, #5c0d1c 0%, #ff0844 60%, #ff7a2f 100%)`,
      mesh: `radial-gradient(at 18% 25%, rgba(106,76,255,.85) 0, transparent 55%),
             radial-gradient(at 78% 18%, rgba(0,180,216,.75) 0, transparent 50%),
             radial-gradient(at 62% 92%, rgba(255,77,157,.65) 0, transparent 48%),
             linear-gradient(140deg, #0f0c29 0%, #1c1b3a 100%)`,
      carbon: `repeating-linear-gradient(45deg, rgba(255,255,255,.05) 0 1px, transparent 1px 7px),
               repeating-linear-gradient(-45deg, rgba(0,0,0,.28) 0 1px, transparent 1px 7px),
               linear-gradient(120deg, #1c2126 0%, #2b333b 100%)`,
      blueprint: `repeating-linear-gradient(0deg, rgba(255,255,255,.09) 0 1px, transparent 1px 20px),
                  repeating-linear-gradient(90deg, rgba(255,255,255,.09) 0 1px, transparent 1px 20px),
                  linear-gradient(120deg, #04263b 0%, #005C97 100%)`,
      chevron: `repeating-linear-gradient(135deg, rgba(255,255,255,.09) 0 10px, transparent 10px 20px),
                repeating-linear-gradient(45deg, rgba(255,255,255,.09) 0 10px, transparent 10px 20px),
                linear-gradient(120deg, #1a2b4a 0%, #37507d 100%)`,
      dots: `radial-gradient(rgba(255,255,255,.20) 1.4px, transparent 1.5px) 0 0 / 16px 16px,
             linear-gradient(120deg, #0f2027 0%, #2c5364 100%)`,
      aurora: `radial-gradient(at 25% 100%, rgba(47,169,104,.85) 0, transparent 60%),
               radial-gradient(at 75% 0%, rgba(0,180,216,.70) 0, transparent 55%),
               linear-gradient(140deg, #06231c 0%, #0d3b34 100%)`,
      ember: `radial-gradient(at 30% 110%, rgba(255,120,40,.80) 0, transparent 55%),
              radial-gradient(at 85% 20%, rgba(255,29,72,.55) 0, transparent 50%),
              linear-gradient(140deg, #1a0b06 0%, #3b1207 100%)`
    };

    /**
     * The old flat-gradient banners were retired when these patterns
     * replaced them, but people already have those ids saved on their
     * player record. Without this map every one of them would silently
     * fall through to the default and quietly lose the choice they made.
     * Resolved on read only - the next time they save, a current id gets
     * written and the alias stops mattering for them.
     */
    const LEGACY_BANNER_ALIASES = {
      sunset: 'ember', ocean: 'blueprint', forest: 'aurora',
      fire: 'smash', royal: 'mesh', candy: 'mesh', midnight: 'dots'
    };

    function resolveBannerId(id) {
      if (!id) return null;
      return BANNER_PRESETS[id] ? id : (LEGACY_BANNER_ALIASES[id] || null);
    }

    /**
     * Page backgrounds are a THIRD independent layer, not the banner
     * reused. On a profile these do different jobs: the background is
     * ambient and sits behind everything at low contrast, the banner is a
     * focal strip. Tying them together means you can't pair a calm
     * backdrop with a loud banner, which is most of the point.
     *
     * These are scaled much larger than the banner patterns, because they
     * cover a whole viewport rather than a 110px strip, and they sit under
     * a heavy veil so they read as texture rather than decoration.
     */
    const BACKGROUND_PRESETS = {
      plain: 'none',
      court: `repeating-linear-gradient(90deg, rgba(120,255,180,.16) 0 3px, transparent 3px 190px),
              repeating-linear-gradient(0deg, rgba(120,255,180,.10) 0 3px, transparent 3px 130px),
              linear-gradient(160deg, #0b3018 0%, #12452a 100%)`,
      nebula: `radial-gradient(at 12% 18%, rgba(106,76,255,.9) 0, transparent 45%),
               radial-gradient(at 88% 12%, rgba(0,180,216,.8) 0, transparent 42%),
               radial-gradient(at 70% 85%, rgba(255,77,157,.7) 0, transparent 40%),
               radial-gradient(at 25% 92%, rgba(47,169,104,.6) 0, transparent 38%),
               linear-gradient(160deg, #0a0a1f 0%, #16162e 100%)`,
      blueprint: `repeating-linear-gradient(0deg, rgba(120,200,255,.13) 0 1px, transparent 1px 44px),
                  repeating-linear-gradient(90deg, rgba(120,200,255,.13) 0 1px, transparent 1px 44px),
                  linear-gradient(160deg, #041d2e 0%, #063b5c 100%)`,
      carbon: `repeating-linear-gradient(45deg, rgba(255,255,255,.045) 0 2px, transparent 2px 12px),
               repeating-linear-gradient(-45deg, rgba(0,0,0,.30) 0 2px, transparent 2px 12px),
               linear-gradient(160deg, #14181c 0%, #242c33 100%)`,
      // Contour lines. repeating-radial-gradient is the only way to get
      // this without shipping an SVG, and it tiles cleanly at this scale.
      topo: `repeating-radial-gradient(circle at 30% 40%, transparent 0 38px, rgba(255,255,255,.07) 38px 40px),
             repeating-radial-gradient(circle at 78% 75%, transparent 0 46px, rgba(255,255,255,.05) 46px 48px),
             linear-gradient(160deg, #12211c 0%, #1d3730 100%)`,
      weave: `repeating-linear-gradient(60deg, rgba(255,255,255,.06) 0 12px, transparent 12px 34px),
              repeating-linear-gradient(-60deg, rgba(255,255,255,.05) 0 12px, transparent 12px 34px),
              linear-gradient(160deg, #1b1526 0%, #2e2340 100%)`,
      glow: `radial-gradient(at 0% 0%, rgba(47,169,104,.75) 0, transparent 48%),
             radial-gradient(at 100% 100%, rgba(0,180,216,.65) 0, transparent 48%),
             linear-gradient(160deg, #0c1712 0%, #101c26 100%)`,
      ember: `radial-gradient(at 15% 105%, rgba(255,120,40,.75) 0, transparent 50%),
              radial-gradient(at 90% 10%, rgba(255,29,72,.55) 0, transparent 45%),
              linear-gradient(160deg, #14090a 0%, #2b1008 100%)`
    };

    /**
     * Swaps the page background to the given player's banner. Every place
     * that changes which card is on screen already routes through
     * renderProfileCardBanner, so this hangs off that rather than being a
     * fourth thing each call site has to remember to call.
     */
    /**
     * Which player's background the Player Card tab is currently showing.
     * Separate from "my own" because the two answer different questions.
     */
    let viewedProfileBackgroundId = null;

    /**
     * Two rules, not one:
     *   - Everywhere except the Player Card, the page wears YOUR OWN
     *     background. It's your app; browsing the Matches tab shouldn't
     *     leave you sitting in someone else's colours.
     *   - On the Player Card, the player you're looking at takes over,
     *     the same way visiting a profile does elsewhere.
     * A guest has no "own" background, so everything outside the card
     * stays plain for them rather than inheriting the last card viewed.
     *
     * The old version applied the viewed player's background globally and
     * never took it back off, which is what made an unrelated tab keep
     * rendering someone else's theme.
     */
    function bgCss(id, url) {
      if (url) return `center / cover no-repeat url("${imageSrc(url)}")`;
      return id ? BACKGROUND_PRESETS[id] : null;
    }

    function updatePageBackground() {
      const onProfileTab = document.getElementById('tab-profile').classList.contains('active');
      const me = allPlayers.find(p => p.player_id === myPlayerId());
      const useViewed = onProfileTab && (viewedProfileBackgroundId || viewedProfileBackgroundUrl);
      const css = useViewed
        ? bgCss(viewedProfileBackgroundId, viewedProfileBackgroundUrl)
        : bgCss(me && me.background_id, me && me.background_url);
      document.documentElement.style.setProperty('--page-bg', css || 'none');
    }

    let viewedProfileBackgroundUrl = null;
    function applyPageBackground(player) {
      viewedProfileBackgroundId = player ? (player.background_id || null) : null;
      viewedProfileBackgroundUrl = player ? (player.background_url || null) : null;
      updatePageBackground();
      prefillEditForm();
    }

    function renderProfileCardBanner(player) {
      // Deliberately outside the early return below: a player with a
      // banner but no avatar still themes the page, and clearing the card
      // must also clear the background rather than stranding the previous
      // player's colours behind an empty view.
      applyPageBackground(player);

      const displayEl = document.getElementById('profile-banner-display');
      const stripEl = document.getElementById('profile-banner-strip');
      const emojiEl = document.getElementById('profile-avatar-emoji');
      const nameEl = document.getElementById('profile-banner-name');
      if (!player || (!player.avatar_id && !player.banner_id && !player.avatar_url && !player.banner_url)) {
        displayEl.style.display = 'none'; return;
      }
      // background_id deliberately not in that test - a background with no
      // banner or avatar still themes the page, it just has no card strip.
      displayEl.style.display = 'block';
      stripEl.style.background = player.banner_url
        ? `center / cover no-repeat url("${imageSrc(player.banner_url)}")`
        : (BANNER_PRESETS[resolveBannerId(player.banner_id)] || BANNER_PRESETS.court);
      // An uploaded photo replaces the emoji entirely rather than sitting
      // behind it, so the circle is either a face or a glyph, never both.
      if (player.avatar_url) {
        emojiEl.textContent = '';
        // The span is normally sized by its emoji glyph. With the text
        // removed it collapses to 0x0, so a background image would have
        // nothing to paint on - it has to be given the circle's size
        // explicitly before it can show a photo.
        emojiEl.style.display = 'block';
        emojiEl.style.width = '100%';
        emojiEl.style.height = '100%';
        emojiEl.style.borderRadius = '50%';
        emojiEl.style.background = `center / cover no-repeat url("${imageSrc(player.avatar_url)}")`;
      } else {
        emojiEl.style.display = '';
        emojiEl.style.width = '';
        emojiEl.style.height = '';
        emojiEl.style.borderRadius = '';
        emojiEl.style.background = '';
        emojiEl.textContent = AVATAR_PRESETS[player.avatar_id] || '';
      }
      nameEl.textContent = formatPlayerLabel(player.name, player.nickname);
    }

    function toggleHeaderMenu() {
      const dropdown = document.getElementById('header-menu-dropdown');
      dropdown.style.display = dropdown.style.display === 'block' ? 'none' : 'block';
    }
    // Close the dropdown when clicking anywhere outside it, standard
    // expected menu behavior.
    document.addEventListener('click', (e) => {
      const dropdown = document.getElementById('header-menu-dropdown');
      const menuBtn = document.getElementById('header-menu-btn');
      if (dropdown.style.display === 'block' && !dropdown.contains(e.target) && e.target !== menuBtn) {
        dropdown.style.display = 'none';
      }
    });

    function openSettingsModal() {
      document.getElementById('settings-modal').style.display = 'flex';
      document.getElementById('settings-status').textContent = '';
      const me = allPlayers.find(p => p.player_id === myPlayerId());
      renderSettingsPickers(me || {});
      // No linked player means nothing to customise - update-my-card
      // would 403 anyway, so don't offer the controls.
      document.getElementById('settings-cosmetics').style.display = me ? 'block' : 'none';
      // The approval sections (requests, finance access) moved to the
      // Reviews & Approvals tab, so nothing to load here anymore.
      prefillEditForm();
    }

    async function loadFinanceAccessList() {
      const listEl = document.getElementById('settings-finance-list');
      listEl.textContent = 'Loading...';
      try {
        // /players returns everyone (not group-scoped like /visible-players),
        // which is what an admin managing finance access needs - otherwise
        // players who don't share a group with the admin never appear.
        const res = await fetch(`${API_BASE_URL}/players`);
        const data = await res.json();
        if (!res.ok) { listEl.textContent = 'Could not load players.'; return; }
        const players = (data.players || []).filter(p => p.claimed)
          .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        if (!players.length) {
          listEl.innerHTML = '<p class="card-sub" style="margin:0;">No players have a linked login yet, so there\'s no one to grant access to. People appear here once they log in and claim a profile.</p>';
          return;
        }
        listEl.innerHTML = players.map(p => {
          const role = p.finance_role || (p.finance_access ? 'write' : 'none');
          const opt = (v, label) => `<option value="${v}"${role === v ? ' selected' : ''}>${label}</option>`;
          return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border);">
            <span>${escapeHtml(p.name)} <span style="color:var(--text-secondary);">(${escapeHtml(p.nickname || '')})</span></span>
            <select style="margin:0; width:auto; padding:4px 8px; font-size:12px;"
                    onchange="setFinanceRole('${p.player_id}', this.value)">
              ${opt('none', 'No access')}${opt('view', 'View')}${opt('write', 'View + Write')}${opt('delete', 'View + Write + Delete')}
            </select>
          </div>`;
        }).join('');
      } catch (e) { listEl.textContent = 'Could not load players.'; }
    }

    // Owner/admin sets a member's per-group finance role directly (the
    // no-request path). Backend: PUT /group-finance-role/{group_id}/{player_id}.
    async function setGroupFinanceRole(groupId, playerId, role) {
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/group-finance-role/${groupId}/${playerId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ finance_role: role })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return false; }
        return true;
      } catch (e) { nwAlert(`Request failed: ${e.message}`); return false; }
    }

    async function setFinanceRole(playerId, role) {
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/finance-access`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ player_id: playerId, role })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadFinanceAccessList();
      } catch (e) { nwAlert(`Request failed: ${e.message}`); }
    }
    function closeSettingsModal() {
      document.getElementById('settings-modal').style.display = 'none';
    }

    function renderSettingsPickers(player) {
      const avatarPicker = document.getElementById('settings-avatar-picker');
      avatarPicker.innerHTML = Object.entries(AVATAR_PRESETS).map(([id, emoji]) =>
        `<button type="button" class="secondary" style="font-size:22px; padding:6px 10px; ${player.avatar_id === id ? 'border-color:var(--court); border-width:2px;' : ''}" onclick="setMyCardField('avatar_id','${id}')">${emoji}</button>`
      ).join('');

      // Swatches are wider than tall so a directional pattern (stripes,
      // chevrons, court lines) is actually legible in the swatch - at 36px
      // square most of these presets look like a single flat colour.
      const swatch = (field, id, css, selected) =>
        `<button type="button" style="width:62px; height:38px; border-radius:6px;
           border:${selected ? '3px solid var(--court)' : '1px solid var(--border)'};
           background:${css.replace(/\s+/g, ' ')}; background-size:auto; cursor:pointer; padding:0;"
           title="${id}" onclick="setMyCardField('${field}','${id}')"></button>`;

      document.getElementById('settings-banner-picker').innerHTML =
        Object.entries(BANNER_PRESETS).map(([id, css]) =>
          swatch('banner_id', id, css, resolveBannerId(player.banner_id) === id)).join('');

      // Show the current photo so "did my upload work?" is answerable
      // without leaving Settings.
      // Your kept uploads, newest first, as a switchable strip. Selecting
      // one is free - it's already stored, so it costs no upload and
      // doesn't push anything off the list.
      renderUploadStrip('avatar', player);
      renderUploadStrip('banner', player);
      renderUploadStrip('background', player);
      renderStoreCosmeticStrip('avatar', player);
      renderStoreCosmeticStrip('banner', player);
      renderStoreCosmeticStrip('background', player);

      document.getElementById('settings-background-picker').innerHTML =
        Object.entries(BACKGROUND_PRESETS).map(([id, css]) =>
          swatch('background_id', id, id === 'plain' ? 'var(--surface-2)' : css,
                 player.background_id === id)).join('');
    }

    /** Asks an admin for access instead of linking immediately. */
    async function submitClaimRequest() {
      const playerId = document.getElementById('complete-profile-claim-select').value;
      const statusEl = document.getElementById('complete-profile-status');
      if (!playerId) { statusEl.textContent = 'Pick which player is you.'; return; }
      statusEl.textContent = 'Sending your request...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/claim-request`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ player_id: playerId })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        finishRequestAndSignOut('Your request to claim that profile has been sent to the admin.');
      } catch (err) {
        statusEl.textContent = `Request failed: ${err.message}`;
      }
    }

    /**
     * After approval, the link lives on the requester's Cognito account -
     * but their current ID token was minted before that and still says
     * unlinked. Forcing a refresh mints a new one carrying the attribute,
     * which is why this is a button rather than something that resolves
     * on its own.
     */
    async function checkApprovalStatus() {
      const el = document.getElementById('approval-check-status');
      el.textContent = 'Checking...';
      const ok = await ensureFreshToken(true);
      if (!ok) { el.textContent = "Couldn't refresh your session - log out and log back in."; return; }
      await loadPlayers();
      if (hasLinkedPlayer()) {
        el.textContent = 'Approved - you are linked.';
      } else {
        el.textContent = 'Not approved yet. Check back once an admin has reviewed it.';
      }
    }

    async function recomputeNow() {
      const statusEl = document.getElementById('recompute-status');
      if (!await nwConfirm('Recompute all ratings, XP, levels and coins from the full match history? This is safe but may take a few seconds.')) return;
      statusEl.textContent = 'Recomputing...';
      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/recompute`, { method: 'POST' });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Done. Reloading players...';
        await loadPlayers();
        updateHeaderCoins();
        statusEl.textContent = 'Done - ratings and XP rebuilt.';
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function loadAppSettings() {
      try {
        const res = await fetch(`${API_BASE_URL}/app-settings`);
        const data = await res.json();
        const cb = document.getElementById('app-instant-create');
        if (cb) cb.checked = !!data.instant_create;
        const xc = document.getElementById('app-xp-public');
        if (xc) xc.checked = !!data.xp_public;
        xpPublic = !!data.xp_public;
        voiceEnabled = !!data.voice_enabled;
        const vc = document.getElementById('app-voice-enabled');
        if (vc) vc.checked = voiceEnabled;
        applyVoiceVisibility();
      } catch (_) { /* leave unchecked */ }
    }

    async function setXpPublic(value) {
      const statusEl = document.getElementById('app-xp-public-status');
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/app-settings`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'xp_public', value })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        xpPublic = value;
        statusEl.textContent = value ? 'Everyone can now see levels, coins, store & quests.' : 'Gamification is admin-only again.';
        updateAuthUI();
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function setVoiceEnabled(value) {
      const statusEl = document.getElementById('app-voice-enabled-status');
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/app-settings`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'voice_enabled', value })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        voiceEnabled = value;
        applyVoiceVisibility();
        statusEl.textContent = value ? 'Voice match entry is ON for everyone.' : 'Voice match entry is admin-only again.';
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function setInstantCreate(value) {
      const statusEl = document.getElementById('app-instant-create-status');
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/app-settings`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: 'instant_create', value })
        });
        statusEl.textContent = res.ok ? (value ? 'Instant create is ON.' : 'Instant create is OFF - new profiles need approval.') : `Error: ${error}`;
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    // ---------- Quests ----------
    async function loadQuests() {
      const el = document.getElementById('quests-list');
      if (!el) return;
      el.innerHTML = 'Loading...';
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/quests`);
        if (!res.ok) { el.innerHTML = '<p class="card-sub">Could not load quests.</p>'; return; }
        const quests = data.quests || [];
        if (!quests.length) { el.innerHTML = '<p class="card-sub" style="margin:0;">No quests this week.</p>'; return; }
        el.innerHTML = quests.map(q => {
          const pct = Math.min(100, Math.round((q.progress / q.target) * 100));
          const rewards = [];
          if (q.reward_xp) rewards.push(`${q.reward_xp} XP`);
          if (q.reward_coins) rewards.push(`${q.reward_coins} 🪙`);
          if (q.reward_cosmetic_id) rewards.push('a cosmetic');
          let action;
          if (q.claimed) action = '<span style="color:#2FA968; font-weight:600; font-size:13px;">Claimed ✓</span>';
          else if (q.complete) action = `<button style="margin:0; padding:4px 12px; font-size:12px;" onclick="claimQuest('${q.quest_id}')">Claim ${rewards.join(' + ')}</button>`;
          else action = `<span style="font-size:12px; opacity:0.6;">Reward: ${rewards.join(' + ')}</span>`;
          return `<div style="padding:10px 0; border-bottom:1px solid var(--border);">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <span style="font-weight:600;">${escapeHtml(q.label)}</span>${action}
            </div>
            <div style="display:flex; align-items:center; gap:8px;">
              <div style="flex:1; height:8px; background:var(--surface-2); border-radius:4px; overflow:hidden;">
                <div style="height:100%; width:${pct}%; background:linear-gradient(90deg, var(--court), #2FA968);"></div>
              </div>
              <span style="font-size:12px; opacity:0.7;">${q.progress}/${q.target}</span>
            </div>
          </div>`;
        }).join('');
      } catch (e) { el.innerHTML = '<p class="card-sub">Could not load quests.</p>'; }
    }

    async function claimQuest(questId) {
      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/quest-claim`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quest_id: questId })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        const bits = [];
        if (data.reward_xp) bits.push(`${data.reward_xp} XP`);
        if (data.reward_coins) bits.push(`${data.reward_coins} coins`);
        if (data.reward_cosmetic) bits.push('a cosmetic');
        nwAlert(`Claimed! You earned ${bits.join(' + ')}.`);
        await loadPlayers(); updateHeaderCoins(); loadQuests(); loadStore();
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    // Admin quest management (in Reviews & Approvals)
    async function loadQuestsAdmin() {
      const listEl = document.getElementById('quests-admin-list');
      if (!listEl) return;
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/quests`);
        if (!res.ok) { listEl.textContent = 'Could not load.'; return; }
        const quests = data.quests || [];
        if (!quests.length) { listEl.innerHTML = '<p class="card-sub" style="margin:0;">No quests yet.</p>'; return; }
        listEl.innerHTML = quests.map(q => {
          const r = [];
          if (q.reward_xp) r.push(`${q.reward_xp}XP`);
          if (q.reward_coins) r.push(`${q.reward_coins}🪙`);
          if (q.reward_cosmetic_id) r.push('cosmetic');
          return `<div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border);">
            <span><strong>${escapeHtml(q.label)}</strong> <span style="opacity:0.6;">→ ${r.join(' + ') || 'no reward'}</span></span>
            <button class="secondary" style="margin:0; padding:2px 8px; font-size:11px;" onclick="deleteQuest('${q.quest_id}')">Delete</button>
          </div>`;
        }).join('');
      } catch (e) { listEl.textContent = 'Could not load.'; }
    }

    async function saveQuest() {
      const statusEl = document.getElementById('quest-save-status');
      const body = {
        type: document.getElementById('quest-type').value,
        target: parseInt(document.getElementById('quest-target').value, 10),
        reward_xp: parseInt(document.getElementById('quest-xp').value, 10) || 0,
        reward_coins: parseInt(document.getElementById('quest-coins').value, 10) || 0,
        reward_cosmetic_id: document.getElementById('quest-cosmetic').value || null
      };
      if (isNaN(body.target) || body.target < 1) { statusEl.textContent = 'Target must be at least 1.'; return; }
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/quests`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Saved.';
        loadQuestsAdmin();
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function deleteQuest(questId) {
      if (!await nwConfirm('Delete this quest? Players who already claimed it keep their reward.')) return;
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/quests`, {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quest_id: questId })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadQuestsAdmin();
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    // ---------- Store ----------
    async function loadStore() {
      const grid = document.getElementById('store-items');
      const balEl = document.getElementById('store-balance');
      if (!grid) return;
      const me = allPlayers.find(p => p.player_id === myPlayerId());
      const balance = me ? Number(me.coins || 0) : 0;
      const owned = (me && me.owned_items) || {};
      if (balEl) balEl.textContent = balance.toLocaleString();
      grid.innerHTML = 'Loading...';
      try {
        const res = await fetch(`${API_BASE_URL}/store`);
        const data = await res.json();
        const items = (data.items || []).filter(i => i.active !== false);
        if (!items.length) { grid.innerHTML = '<p class="card-sub">Nothing in the store yet.</p>'; return; }
        grid.innerHTML = items.map(i => {
          const ownsIt = owned[i.item_id];
          const isPerk = i.type === 'perk';
          const canAfford = balance >= i.cost;
          let btn;
          if (!isPerk && ownsIt) {
            btn = `<button class="secondary" disabled style="width:100%; opacity:0.6;">Owned</button>`;
          } else {
            const label = isPerk && ownsIt ? `Buy again (own ${ownsIt})` : 'Buy';
            btn = `<button ${canAfford ? '' : 'disabled'} style="width:100%; ${canAfford ? '' : 'opacity:0.5;'}" onclick="buyStoreItem('${i.item_id}')">${label} — ${i.cost} 🪙</button>`;
          }
          return `<div style="border:1px solid var(--border); border-radius:10px; padding:14px; background:var(--surface-2);">
            ${i.image_url ? `<img src="${imageSrc(i.image_url)}" alt="" style="width:100%; height:120px; object-fit:cover; border-radius:8px; margin-bottom:10px;">` : ''}
            <div style="font-weight:700; margin-bottom:4px;">${escapeHtml(i.name)}</div>
            <div style="font-size:11px; text-transform:uppercase; opacity:0.6; margin-bottom:10px;">${i.type}</div>
            ${btn}
          </div>`;
        }).join('');
      } catch (e) { grid.innerHTML = 'Could not load the store.'; }
    }

    async function buyStoreItem(itemId) {
      if (!await nwConfirm('Spend coins on this item?')) return;
      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/store-purchase`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item_id: itemId })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        await loadPlayers();          // refresh coin balance + owned
        updateHeaderCoins();
        loadStore();
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    // Admin catalog management (in Reviews & Approvals)
    function onStoreImagePick(input) {
      const img = document.getElementById('store-image-preview');
      if (!img) return;
      const f = input.files && input.files[0];
      if (f) { img.src = URL.createObjectURL(f); img.style.display = 'block'; }
      else { img.style.display = 'none'; }
    }

    async function loadStoreAdmin() {
      const listEl = document.getElementById('store-admin-list');
      if (!listEl) return;
      try {
        const res = await fetch(`${API_BASE_URL}/store`);
        const data = await res.json();
        const items = data.items || [];
        if (!items.length) { listEl.innerHTML = '<p class="card-sub" style="margin:0;">No items yet.</p>'; return; }
        listEl.innerHTML = items.map(i => `
          <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid var(--border);">
            <span style="display:flex; align-items:center; gap:8px;">
              ${i.image_url ? `<img src="${imageSrc(i.image_url)}" alt="" style="width:34px; height:34px; object-fit:cover; border-radius:6px; border:1px solid var(--border);">` : ''}
              <span><strong>${escapeHtml(i.name)}</strong> — ${i.cost} 🪙 <span style="opacity:0.6;">(${i.type})</span></span>
            </span>
            <button class="secondary" style="margin:0; padding:2px 8px; font-size:11px;" onclick="deleteStoreItem('${i.item_id}')">Delete</button>
          </div>`).join('');
      } catch (e) { listEl.textContent = 'Could not load items.'; }
    }

    const STORE_IMAGE_EFFECTS = ['avatar_frame', 'banner_image', 'background_image', 'profile_effect', 'card_frame'];

    function onStoreTypeChange() {
      const type = document.getElementById('store-item-type').value;
      // Show only the matching effect optgroup by disabling the other's options.
      document.querySelectorAll('#effect-group-cosmetic option').forEach(o => o.disabled = (type !== 'cosmetic'));
      document.querySelectorAll('#effect-group-perk option').forEach(o => o.disabled = (type !== 'perk'));
      // Jump selection to the first enabled option.
      const sel = document.getElementById('store-item-effect');
      const firstEnabled = Array.from(sel.options).find(o => !o.disabled);
      if (firstEnabled) sel.value = firstEnabled.value;
      onStoreEffectChange();
    }

    function onStoreEffectChange() {
      const eff = document.getElementById('store-item-effect').value;
      const needsImage = STORE_IMAGE_EFFECTS.includes(eff);
      document.getElementById('store-image-row').style.display = needsImage ? 'block' : 'none';
      document.getElementById('store-effect-value-row').style.display = needsImage ? 'none' : 'block';
    }

    async function uploadStoreImage(file) {
      const statusEl = document.getElementById('store-image-status');
      statusEl.textContent = 'Uploading...';
      const buf = await file.arrayBuffer();
      const hashBuf = await crypto.subtle.digest('SHA-256', buf);
      const fingerprint = Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 32);
      const { res, data, error } = await authedFetch(`${API_BASE_URL}/upload-url`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: 'store', content_type: file.type, fingerprint })
      });
      if (!res.ok) { statusEl.textContent = `Error: ${error}`; return null; }
      const put = await fetch(data.upload_url, { method: 'PUT', headers: { 'Content-Type': file.type }, body: file });
      if (!put.ok) { statusEl.textContent = 'Upload failed.'; return null; }
      statusEl.textContent = 'Uploaded ✓';
      return data.key;
    }

    async function saveStoreItem() {
      const statusEl = document.getElementById('store-save-status');
      const effKind = document.getElementById('store-item-effect').value;
      const type = document.getElementById('store-item-type').value;
      let effect = { kind: effKind };
      let image_url = null;

      if (STORE_IMAGE_EFFECTS.includes(effKind)) {
        const fileInput = document.getElementById('store-item-image');
        const existingKey = document.getElementById('store-item-image-key').value;
        if (fileInput.files[0]) {
          const key = await uploadStoreImage(fileInput.files[0]);
          if (!key) { statusEl.textContent = 'Image upload failed.'; return; }
          image_url = key;
        } else if (existingKey) {
          image_url = existingKey;
        } else {
          statusEl.textContent = 'Pick an image for this cosmetic.'; return;
        }
        effect.image_url = image_url;
      } else {
        const val = document.getElementById('store-item-value').value.trim();
        if (val) effect.value = val;
      }

      const body = {
        name: document.getElementById('store-item-name').value.trim(),
        type, cost: parseInt(document.getElementById('store-item-cost').value, 10),
        effect, image_url
      };
      if (!body.name || isNaN(body.cost)) { statusEl.textContent = 'Name and cost required.'; return; }
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/store`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Saved.';
        document.getElementById('store-item-name').value = '';
        document.getElementById('store-item-cost').value = '';
        document.getElementById('store-item-image-key').value = '';
        const _pv = document.getElementById('store-image-preview'); if (_pv) { _pv.src = ''; _pv.style.display = 'none'; }
        loadStoreAdmin();
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function deleteStoreItem(itemId) {
      if (!await nwConfirm('Delete this store item? Players who already bought it keep it.')) return;
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/store`, {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item_id: itemId })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadStoreAdmin();
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    async function loadEventsAdmin() {
      const listEl = document.getElementById('events-list');
      if (!listEl) return;
      try {
        const res = await fetch(`${API_BASE_URL}/events`);
        const data = await res.json();
        const events = data.events || [];
        if (!events.length) { listEl.innerHTML = '<p class="card-sub" style="margin:0;">No events yet.</p>'; return; }
        const today = new Date().toLocaleDateString('en-CA');
        listEl.innerHTML = events.map(e => {
          const active = e.start_date <= today && today <= e.end_date;
          return `<div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border);">
            <span>${active ? '🟢 ' : ''}<strong>${escapeHtml(e.name)}</strong> — ${e.xp_multiplier}× XP <span style="opacity:0.6;">(${e.start_date} to ${e.end_date})</span></span>
            <span>
              <button class="secondary" style="margin:0; padding:2px 8px; font-size:11px;" onclick='editEvent(${JSON.stringify(e)})'>Edit</button>
              <button class="secondary" style="margin:0; padding:2px 8px; font-size:11px;" onclick="deleteEvent('${e.event_id}')">Delete</button>
            </span>
          </div>`;
        }).join('');
      } catch (e) { listEl.textContent = 'Could not load events.'; }
    }

    function editEvent(e) {
      document.getElementById('event-edit-id').value = e.event_id;
      document.getElementById('event-name').value = e.name;
      document.getElementById('event-start').value = e.start_date;
      document.getElementById('event-end').value = e.end_date;
      document.getElementById('event-mult').value = e.xp_multiplier;
    }

    async function saveEvent() {
      const statusEl = document.getElementById('event-save-status');
      const body = {
        event_id: document.getElementById('event-edit-id').value || undefined,
        name: document.getElementById('event-name').value.trim(),
        start_date: document.getElementById('event-start').value,
        end_date: document.getElementById('event-end').value,
        xp_multiplier: parseFloat(document.getElementById('event-mult').value)
      };
      if (!body.name || !body.start_date || !body.end_date) { statusEl.textContent = 'Name and dates required.'; return; }
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/events`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Saved.';
        document.getElementById('event-edit-id').value = '';
        document.getElementById('event-name').value = '';
        loadEventsAdmin(); refreshEventBanner();
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    async function deleteEvent(eventId) {
      if (!await nwConfirm('Delete this event? Past matches keep the XP they already earned until the next recompute.')) return;
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/events`, {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ event_id: eventId })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadEventsAdmin(); refreshEventBanner();
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    // Public: a small banner shown to everyone when an event is live.
    async function refreshEventBanner() {
      const bar = document.getElementById('event-banner');
      if (!bar) return;
      try {
        const res = await fetch(`${API_BASE_URL}/events`);
        const data = await res.json();
        const today = new Date().toLocaleDateString('en-CA');
        const active = (data.events || []).find(e => e.start_date <= today && today <= e.end_date);
        if (active) {
          bar.innerHTML = `🎉 <strong>${escapeHtml(active.name)}</strong> is live — ${active.xp_multiplier}× XP on every match until ${active.end_date}!`;
          bar.style.display = 'block';
        } else { bar.style.display = 'none'; }
      } catch (_) { bar.style.display = 'none'; }
    }

    let _auditPlayers = [];   // cached player list for the re-link picker

    async function loadClaimAudit() {
      const el = document.getElementById('claim-audit-result');
      const countEl = document.getElementById('review-audit-count');
      if (!el) return;
      el.textContent = 'Loading...';
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/claim-audit`);
        if (!res.ok) { el.textContent = `Error: ${data.error || 'could not load'}`; return; }
        _auditPlayers = (await (await fetch(`${API_BASE_URL}/players`)).json()).players || [];
        const problems = data.problems || [];
        const accounts = data.accounts || [];
        const brokenAccts = accounts.filter(a => a.issue !== 'healthy');
        if (countEl) countEl.textContent = (problems.length + brokenAccts.length) || '';

        const pickerOptions = _auditPlayers
          .slice().sort((a, b) => (a.name || '').localeCompare(b.name || ''))
          .map(p => `<option value="${p.player_id}">${escapeHtml(p.name)} (${escapeHtml(p.nickname || '')})</option>`).join('');

        let html = '';
        if (!problems.length && !brokenAccts.length) {
          html += '<p style="color:var(--court,#2fa968);">All accounts are healthily linked.</p>';
        }

        // Accounts with no profile / dangling link (Suren's case sits here).
        if (brokenAccts.length) {
          html += '<h4 style="font-size:13px;margin:6px 0;">Accounts needing a profile link</h4>';
          brokenAccts.forEach(a => {
            const tag = a.issue === 'no_profile' ? 'no profile linked' : 'links to a deleted player';
            const uname = encodeURIComponent(a.username || '');
            html += `<div style="padding:8px 0;border-bottom:1px solid var(--border);">
              <div><strong>${escapeHtml(a.email || a.username || '')}</strong> <span style="color:#c0392b;">${tag}</span> <span style="color:var(--text-secondary);font-size:11px;">(${a.status})</span></div>
              <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
                <select id="audit-pick-${uname}" style="max-width:220px;"><option value="">Link to player...</option>${pickerOptions}</select>
                <button class="secondary" style="margin:0;padding:4px 10px;font-size:12px;" onclick="relinkAccount('${uname}')">Link</button>
              </div></div>`;
          });
        }

        // Player rows whose email linkage is broken.
        if (problems.length) {
          html += '<h4 style="font-size:13px;margin:14px 0 6px;">Profiles with linkage problems</h4>';
          problems.forEach(pr => {
            const uname = encodeURIComponent(pr.username || '');
            let actions = '';
            if (pr.kind === 'claimed_unlinked' && pr.username) {
              actions = `<button class="secondary" style="margin:0;padding:4px 10px;font-size:12px;" onclick="relinkAccount('${uname}','${pr.player_id}')">Link account &rarr; this profile</button>`;
            } else if (pr.kind === 'misstamp' && pr.username) {
              actions = `<button class="secondary" style="margin:0;padding:4px 10px;font-size:12px;" onclick="unlinkAndStrip('${uname}','${pr.player_id}')">Strip wrong email + unlink</button>`;
            }
            html += `<div style="padding:8px 0;border-bottom:1px solid var(--border);">
              <div><strong>${escapeHtml(pr.player_label)}</strong> &mdash; <span style="color:#c0392b;">${pr.kind.replace('_',' ')}</span></div>
              <div style="font-size:12px;color:var(--text-secondary);margin:2px 0 6px;">${escapeHtml(pr.detail)}</div>
              ${actions}</div>`;
          });
        }

        // Full healthy list, collapsed.
        const healthy = accounts.filter(a => a.issue === 'healthy');
        if (healthy.length) {
          html += `<details style="margin-top:14px;"><summary style="font-size:12px;color:var(--text-secondary);">All ${healthy.length} healthy links</summary>`;
          html += healthy.map(a => `<div class="member-row"><span>${escapeHtml(a.email || a.username)} &rarr; ${escapeHtml(a.linked_player || '')}</span><button class="secondary" style="margin:0;padding:2px 8px;font-size:11px;" onclick="unlinkAccount('${encodeURIComponent(a.username)}')">Unlink</button></div>`).join('');
          html += '</details>';
        }
        el.innerHTML = html;
      } catch (e) { el.textContent = `Could not load: ${e.message}`; }
    }

    async function relinkAccount(usernameEnc, presetPlayerId) {
      const username = decodeURIComponent(usernameEnc);
      const playerId = presetPlayerId || (document.getElementById(`audit-pick-${usernameEnc}`) || {}).value;
      if (!playerId) { nwAlert('Pick a player to link to first.'); return; }
      if (!await nwConfirm('Link this account to the selected profile? The user must log out and back in afterwards.')) return;
      await _claimAuditAction({ action: 'link', username, player_id: playerId });
    }

    async function unlinkAccount(usernameEnc) {
      if (!await nwConfirm('Unlink this account from its profile? They will see no profile until re-linked or they re-claim.')) return;
      await _claimAuditAction({ action: 'unlink', username: decodeURIComponent(usernameEnc) });
    }

    async function unlinkAndStrip(usernameEnc, playerId) {
      if (!await nwConfirm('Strip the wrong email off this profile and unlink? This frees the profile to be claimed correctly.')) return;
      await _claimAuditAction({ action: 'unlink', username: decodeURIComponent(usernameEnc), player_id: playerId, strip_player_email: true });
    }

    async function _claimAuditAction(bodyObj) {
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/claim-audit`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bodyObj)
        });
        if (!res.ok) { nwAlert(`Error: ${data.error || 'action failed'}`); return; }
        if (data.note) nwAlert(data.note);
        loadClaimAudit();
      } catch (e) { nwAlert(`Request failed: ${e.message}`); }
    }

    async function loadUnconfirmedUsers() {
      const listEl = document.getElementById('unconfirmed-users-list');
      const countEl = document.getElementById('review-unconfirmed-count');
      if (!listEl) return;
      listEl.textContent = 'Loading...';
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/unconfirmed-users`);
        if (!res.ok) { listEl.textContent = `Error: ${data.error || 'could not load'}`; return; }
        const users = data.unconfirmed || [];
        if (countEl) countEl.textContent = users.length ? String(users.length) : '';
        if (!users.length) {
          listEl.innerHTML = '<p class="card-sub" style="margin:0;">No unconfirmed sign-ups. Everyone who signed up has verified their email.</p>';
          return;
        }
        listEl.innerHTML = users.map(u => {
          const when = u.created_at ? new Date(u.created_at).toLocaleDateString() : '';
          const email = escapeHtml(u.email || u.username || '');
          const uname = encodeURIComponent(u.username || u.email || '');
          return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border);">
            <span>${email} <span style="color:var(--text-secondary);">signed up ${when}</span></span>
            <button class="secondary" type="button" style="margin:0; padding:4px 10px; font-size:12px;" onclick="deleteUnconfirmedUser('${uname}', '${email}')">Delete</button>
          </div>`;
        }).join('');
      } catch (e) { listEl.textContent = `Could not load: ${e.message}`; }
    }

    async function deleteUnconfirmedUser(username, email) {
      if (!await nwConfirm(`Delete the unconfirmed sign-up for ${email}?\n\nThey'll be able to register again with this email. This only works on accounts that never verified.`)) return;
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/unconfirmed-users`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: decodeURIComponent(username) })
        });
        if (!res.ok) { nwAlert(`Error: ${data.error || 'could not delete'}`); return; }
        loadUnconfirmedUsers();
      } catch (e) { nwAlert(`Request failed: ${e.message}`); }
    }

    async function loadClaimRequests() {
      const listEl = document.getElementById('settings-requests-list');
      listEl.textContent = 'Loading...';
      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/claim-requests`);
        if (!res.ok) { listEl.textContent = `Error: ${error}`; return; }
        const pending = data.pending || [];
        const badge = document.getElementById('review-requests-count');
        if (badge) badge.textContent = pending.length ? String(pending.length) : '';
        if (!pending.length) {
          listEl.innerHTML = '<p class="card-sub" style="margin:0;">No requests waiting.</p>';
          return;
        }
        listEl.innerHTML = pending.map(r => {
          const target = `${escapeHtml(r.player_name || '')} (${escapeHtml(r.player_nickname || '')})`;
          const what = {
            delete_player: `wants to <strong style="color:var(--smash);">delete</strong> ${target}`,
            new_profile:   `wants a <strong>new profile</strong>: ${target}`,
            finance_access: `wants <strong>${escapeHtml(r.requested_role || 'view')}</strong> finance access (${target})`,
            match_delete: `wants to <strong style="color:var(--smash);">delete match</strong> ${escapeHtml(r.match_label || '')}${r.reason ? ' - "' + escapeHtml(r.reason) + '"' : ''}`,
            match_edit: `wants to <strong>correct</strong> ${escapeHtml(r.match_label || '')} to <strong>${r.new_score_a}-${r.new_score_b}</strong>${r.reason ? ' - "' + escapeHtml(r.reason) + '"' : ''}`,
            edit_own_name: `wants to rename ${target} to <strong>${escapeHtml(r.new_name || '')} (${escapeHtml(r.new_nickname || '')})</strong>`,
            claim:         `wants to be ${target}`
          }[r.type || 'claim'];
          const reason = r.reason ? `<div style="color:var(--text-secondary); font-size:12px; font-style:italic;">"${escapeHtml(r.reason)}"</div>` : '';
          return `
          <div style="border:1px solid var(--border); border-radius:var(--radius); padding:10px; margin-bottom:8px;">
            <div style="font-weight:600;">${escapeHtml(r.requester_email)}</div>
            <div style="color:var(--text-secondary); font-size:12px; margin-bottom:8px;">${what}</div>
            ${reason}
            <button type="button" style="margin:0; padding:5px 12px; font-size:12px;"
                    onclick="decideClaimRequest('${r.request_id}','approve','${r.type || 'claim'}')">Approve</button>
            <button type="button" class="secondary" style="margin:0; padding:5px 12px; font-size:12px;"
                    onclick="decideClaimRequest('${r.request_id}','reject','${r.type || 'claim'}')">Reject</button>
          </div>`; }).join('');
      } catch (err) {
        listEl.textContent = `Request failed: ${err.message}`;
      }
    }

    async function decideClaimRequest(requestId, action, requestType) {
      if (action === 'approve') {
        const msg = {
          delete_player: 'Approve this deletion?\n\nThe player record and any login linked to it are removed permanently. Their match history stays, but the player is gone.',
          new_profile:   'Approve this new profile?\n\nA new player is created and linked to their login. They become a full member and can add players and record matches.',
          finance_access: 'Grant finance access?\n\nThey will be able to view the Finance tab.',
          match_delete: 'Approve this match deletion?\n\nThe match is removed and every rating recomputed from the remaining history.',
          match_edit: 'Approve this score correction?\n\nThe match is updated and every rating recomputed.',
          edit_own_name: 'Approve this rename?\n\nTheir display name and nickname change everywhere, including past matches.',
          claim:         'Approve this request?\n\nThis links their login to that player permanently - their match history and rating become theirs.'
        }[requestType || 'claim'];
        if (!await nwConfirm(msg)) return;
      }
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/claim-request-decide`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request_id: requestId, action })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        if (action === 'reject' && (requestType === 'claim' || requestType === 'new_profile')) {
          nwAlert('Rejected. Their login has been removed, so that email address is free to sign up again.');
        }
        await loadClaimRequests();
        await loadPlayers();
        // A finance_access approval flips a flag the finance list shows, so
        // refresh it too - otherwise the newly-granted person doesn't
        // appear as having access until Settings is reopened.
        if (requestType === 'finance_access' && typeof loadFinanceAccessList === 'function') {
          loadFinanceAccessList();
        }
      } catch (err) {
        nwAlert(`Request failed: ${err.message}`);
      }
    }

    /** Request data is user-supplied (email addresses, names), and it is
     *  rendered into innerHTML above - so it gets escaped on the way in. */
    function escapeHtml(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // ---------- image uploads ----------

    /**
     * Resize and re-encode in the browser BEFORE uploading. A modern phone
     * camera produces 4-12MB files; an avatar is displayed at 96px and a
     * banner at roughly 1200px wide. Uploading the original would waste
     * storage, waste everyone's mobile data on every page load, and take
     * far longer on court wifi - for pixels nobody ever sees.
     *
     * Avatars are centre-cropped square, because they render as circles and
     * a letterboxed portrait looks broken in one.
     */
    function resizeImage(file, kind) {
      const MAX = kind === 'avatar' ? 512 : 1400;
      return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(file);
        img.onload = () => {
          URL.revokeObjectURL(url);
          const canvas = document.createElement('canvas');
          const ctx = canvas.getContext('2d');
          if (kind === 'avatar') {
            const side = Math.min(img.width, img.height);   // centre crop to square
            canvas.width = canvas.height = Math.min(side, MAX);
            ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side,
                          0, 0, canvas.width, canvas.height);
          } else {
            const scale = Math.min(1, MAX / img.width);
            canvas.width = Math.round(img.width * scale);
            canvas.height = Math.round(img.height * scale);
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          }
          canvas.toBlob(b => b ? resolve(b) : reject(new Error('could not process that image')),
                        'image/jpeg', 0.85);
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('that file is not a readable image')); };
        img.src = url;
      });
    }

    /**
     * Animated WebP carries an 'ANIM' chunk near the top of the file. We
     * only need to detect WebP here - the backend allowlist rejects GIF,
     * and JPEG/PNG can't animate - so a cheap scan of the first few KB for
     * the ANIM marker is enough to know whether the canvas re-encode would
     * destroy motion.
     */
    async function isAnimatedImage(file) {
      if (file.type !== 'image/webp') return false;
      try {
        const head = new Uint8Array(await file.slice(0, 4096).arrayBuffer());
        for (let i = 0; i + 4 <= head.length; i++) {
          if (head[i] === 0x41 && head[i + 1] === 0x4E &&
              head[i + 2] === 0x49 && head[i + 3] === 0x4D) return true;  // "ANIM"
        }
      } catch (_) { /* if we can't read it, treat as static and let resize try */ }
      return false;
    }

    async function uploadCardImage(kind, fileInput) {
      const statusEl = document.getElementById('settings-status');
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (!/^image\//.test(file.type)) { statusEl.textContent = 'Pick an image file.'; return; }

      try {
        statusEl.textContent = 'Processing image...';
        // Animated WebP must skip resizeImage: that path draws onto a
        // canvas and re-encodes to JPEG, and a canvas holds a single frame
        // - so an animated upload would arrive as a frozen still. Send the
        // original bytes with their real type instead. Static images still
        // get centre-cropped/scaled and shrunk to JPEG as before. The
        // circular/cover CSS crops an animated avatar at display time, so
        // skipping the square canvas crop here costs nothing visually.
        const animated = await isAnimatedImage(file);
        const blob = animated ? file : await resizeImage(file, kind);
        const contentType = animated ? 'image/webp' : 'image/jpeg';

        statusEl.textContent = 'Getting upload permission...';
        // Content hash, not a random id. Re-uploading the same photo
        // produces the same key, so it overwrites itself instead of
        // filling the player's slots with copies of one image - and the
        // rotation list treats it as a reselect rather than a new upload.
        const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
        const hash = Array.from(new Uint8Array(digest)).slice(0, 16)
          .map(b => b.toString(16).padStart(2, '0')).join('');

        const { res: urlRes, data: urlData, error } = await authedFetch(`${API_BASE_URL}/upload-url`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, content_type: contentType, fingerprint: hash })
        });
        if (!urlRes.ok) { statusEl.textContent = `Error: ${error}`; return; }

        statusEl.textContent = 'Uploading...';
        // Straight to S3, not through our API. Content-Type must match what
        // the presigned URL was signed for or S3 rejects the signature.
        const putRes = await fetch(urlData.upload_url, {
          method: 'PUT',
          headers: { 'Content-Type': contentType },
          body: blob
        });
        if (!putRes.ok) { statusEl.textContent = `Upload failed (HTTP ${putRes.status}).`; return; }

        const urlField = { avatar: 'avatar_url', banner: 'banner_url', background: 'background_url' }[kind];
        await setMyCardField(urlField, urlData.key);
        fileInput.value = '';
      } catch (err) {
        statusEl.textContent = err.message;
      }
    }

    /** Uploaded images beat presets. Stored as a key, served same-origin
     *  through CloudFront, so no absolute domain is baked into the data. */
    function imageSrc(key) { return key ? `/${key}` : null; }


    // Cache the store catalog for the session so the customizer can resolve
    // a player's owned_items into equippable cosmetics without re-fetching.
    let _storeCatalogCache = null;
    async function loadStoreCatalogOnce() {
      if (_storeCatalogCache) return _storeCatalogCache;
      try { const r = await fetch(`${API_BASE_URL}/store`); const d = await r.json(); _storeCatalogCache = d.items || []; }
      catch (e) { _storeCatalogCache = []; }
      return _storeCatalogCache;
    }

    /** Shows store cosmetics the player OWNS for this slot, as equippable
     *  swatches - the bridge that was missing between the store and the
     *  profile customizer. */
    async function renderStoreCosmeticStrip(kind, player) {
      const anchor = document.getElementById(`settings-${kind}-uploads`);
      if (!anchor) return;
      let host = document.getElementById(`settings-${kind}-store`);
      if (!host) { host = document.createElement('div'); host.id = `settings-${kind}-store`; anchor.parentNode.insertBefore(host, anchor.nextSibling); }
      const want = { avatar: 'avatar_frame', banner: 'banner_image', background: 'background_image' }[kind];
      const owned = (player && player.owned_items) || {};
      const items = (await loadStoreCatalogOnce()).filter(i => owned[i.item_id] && (i.effect || {}).kind === want && i.image_url);
      if (!items.length) { host.innerHTML = ''; return; }
      const urlField = { avatar: 'avatar_url', banner: 'banner_url', background: 'background_url' }[kind];
      const current = player && player[urlField];
      const shape = kind === 'avatar' ? 'width:44px; height:44px; border-radius:50%;' : 'width:72px; height:40px; border-radius:6px;';
      host.innerHTML =
        `<p class="card-sub" style="margin:8px 0 6px;">From the store (${items.length})</p>` +
        items.map(i =>
          `<button type="button" title="${escapeHtml(i.name)}"
             style="${shape} padding:0; margin:0 6px 6px 0; cursor:pointer;
                    border:${i.image_url === current ? '3px solid var(--court)' : '1px solid var(--border)'};
                    background:center / cover no-repeat url('${imageSrc(i.image_url)}');"
             onclick="setMyCardField('${urlField}','${i.image_url}')"></button>`
        ).join('');
    }

    /** Renders a player's kept custom uploads as a small switcher. */
    function renderUploadStrip(kind, player) {
      const el = document.getElementById(`settings-${kind}-uploads`);
      const keys = (player[`${kind}_uploads`] || []).filter(Boolean);
      const current = player[`${kind}_url`];
      if (!keys.length) { el.innerHTML = ''; return; }
      const shape = kind === 'avatar'
        ? 'width:44px; height:44px; border-radius:50%;'
        : 'width:72px; height:40px; border-radius:6px;';
      el.innerHTML =
        `<p class="card-sub" style="margin:0 0 6px;">Your uploads (${keys.length} of 3 kept - a fourth replaces the oldest)</p>` +
        keys.map(k =>
          `<button type="button" title="Use this"
             style="${shape} padding:0; margin:0 6px 6px 0; cursor:pointer;
                    border:${k === current ? '3px solid var(--court)' : '1px solid var(--border)'};
                    background:center / cover no-repeat url('${imageSrc(k)}');"
             onclick="setMyCardField('${kind}_url','${k}')"></button>`
        ).join('') +
        `<button type="button" class="secondary" style="margin:0 0 6px; padding:4px 10px; font-size:12px; vertical-align:top;"
           onclick="setMyCardField('${kind}_url','')">Use a preset instead</button>`;
    }

    /**
     * The VS card. Two banners diffused into each other with a mask
     * gradient rather than a hard clip, so they genuinely blend at the
     * seam instead of butting against a cut edge.
     *
     * Reads a SNAPSHOT when the fixture carries one: a tournament records
     * each player's avatar and banner at creation time, so a bracket keeps
     * the look it had on the day even if someone changes their photo
     * afterwards. Falls back to live player data, then to presets, so it
     * always renders something.
     */
    function vsPlayerVisual(pid, snapshot) {
      const snap = (snapshot && snapshot[pid]) || null;
      const live = allPlayers.find(p => p.player_id === pid) || {};
      const src = snap || live;
      return {
        name: live.nickname || live.name || '',
        avatarUrl: src.avatar_url ? imageSrc(src.avatar_url) : null,
        avatarEmoji: AVATAR_PRESETS[src.avatar_id] || '',
        hasUpload: !!src.banner_url,
        hasBanner: !!(src.banner_url || resolveBannerId(src.banner_id)),
        banner: src.banner_url
          ? `center / cover no-repeat url("${imageSrc(src.banner_url)}")`
          : (BANNER_PRESETS[resolveBannerId(src.banner_id)] || BANNER_PRESETS.court)
      };
    }

    function vsAvatarHtml(v, isWinner) {
      const bg = v.avatarUrl ? `background-image:url('${v.avatarUrl}');` : '';
      return `<div class="vsc-pl">
        <div class="vsc-av${isWinner ? ' vsc-av-won' : ''}" style="${bg}">${v.avatarUrl ? '' : escapeHtml(v.avatarEmoji)}</div>
        <div class="vsc-nm">${escapeHtml(v.name)}</div></div>`;
    }

    const VS_CUP_SVG = `<svg width="34" height="34" viewBox="0 0 24 24" fill="none">
      <path d="M6 3h12v5a6 6 0 0 1-12 0V3Z" fill="#F5C542"/>
      <path d="M6 4H3.5v1.5A3.5 3.5 0 0 0 7 9M18 4h2.5v1.5A3.5 3.5 0 0 1 17 9" stroke="#F5C542" stroke-width="1.6" stroke-linecap="round"/>
      <path d="M12 14v3M8.5 20h7l-.8-2.2a1.2 1.2 0 0 0-1.1-.8h-3.2a1.2 1.2 0 0 0-1.1.8L8.5 20Z" stroke="#F5C542" stroke-width="1.6" stroke-linejoin="round"/>
    </svg>`;

    /** A team's banner is whichever member actually set one - preferring an
     *  uploaded image over a preset. Taking members[0] blindly meant a pair
     *  fell back to the default whenever the first-listed player happened
     *  to have no banner, even though their partner had a custom one. */
    function teamBanner(side) {
      const uploaded = side.find(v => v.hasUpload);
      if (uploaded) return uploaded.banner;
      const chosen = side.find(v => v.hasBanner);
      return (chosen || side[0]).banner;
    }

    /** Games store scores as score_a/score_b. Reading `.a` silently gave
     *  undefined, which made the card skip the score block entirely - so
     *  neither the card nor the removed row showed anything. Accepts the
     *  short form too in case another payload uses it. */
    function gameScore(game, side) {
      if (!game) return undefined;
      const v = game[`score_${side}`] !== undefined ? game[`score_${side}`] : game[side];
      return v === undefined || v === null ? undefined : v;
    }

    function renderVsCard(idsA, idsB, opts = {}) {
      const snap = opts.snapshot || null;
      const a = (idsA || []).map(id => vsPlayerVisual(id, snap));
      const b = (idsB || []).map(id => vsPlayerVisual(id, snap));
      if (!a.length || !b.length) return '';
      const won = (side) => opts.winner === side ? ' vsc-won' : '';
      const scoreHtml = (opts.scoreA === undefined || opts.scoreA === null) ? '' : `
        <div class="vsc-score vsc-score-a${won('a')}">${escapeHtml(String(opts.scoreA))}${opts.winner === 'a' ? '<span class="vsc-w">W</span>' : ''}</div>
        <div class="vsc-score vsc-score-b${won('b')}">${escapeHtml(String(opts.scoreB))}${opts.winner === 'b' ? '<span class="vsc-w">W</span>' : ''}</div>`;
      const centre = opts.isFinal
        ? `<div class="vsc-cup">${VS_CUP_SVG}<div class="vsc-cupl">FINAL</div></div>`
        : `<div class="vsc-badge">VS</div>`;
      const goldEdge = opts.isFinal
        ? `<div class="vsc-layer" style="box-shadow:inset 0 0 0 2px rgba(245,197,66,.55); border-radius:var(--radius);"></div>` : '';
      return `<div class="vsc">
        <div class="vsc-layer" style="background:${teamBanner(b)};"></div>
        <div class="vsc-layer" style="background:${teamBanner(a)};
             -webkit-mask-image:linear-gradient(115deg,#000 36%,transparent 64%);
             mask-image:linear-gradient(115deg,#000 36%,transparent 64%);"></div>
        <div class="vsc-layer" style="background:linear-gradient(115deg,transparent 42%,rgba(0,0,0,.30) 50%,transparent 58%);"></div>
        ${goldEdge}
        <div style="position:absolute; left:14px; top:16px; display:flex; gap:8px; z-index:4;">${a.map(v => vsAvatarHtml(v, opts.winner === 'a')).join('')}</div>
        <div style="position:absolute; right:14px; bottom:16px; display:flex; gap:8px; z-index:4;">${b.map(v => vsAvatarHtml(v, opts.winner === 'b')).join('')}</div>
        ${scoreHtml}
        ${centre}
      </div>`;
    }

    /** Fixture sides may carry a single id or a doubles pair, depending on
     *  the format - normalise so callers don't have to care. */
    function vsSideIds(side) {
      if (!side) return [];
      // A doubles fixture side is a synthetic TEAM: `player_id` is the
      // pair's own id, and `members` holds the two real player ids. Only
      // the latter resolve to avatars, so they come first. Singles has no
      // `members`, and there `player_id` is the real one.
      if (Array.isArray(side.members) && side.members.length) return side.members;
      return side.player_ids || side.ids || (side.player_id ? [side.player_id] : []);
    }

    async function setMyCardField(field, value) {
      const statusEl = document.getElementById('settings-status');
      statusEl.textContent = 'Saving...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/update-my-card`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [field]: value })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Saved.';

        // Purely cosmetic, and already confirmed saved server-side - so
        // patch the local cache and re-render from it instead of going
        // back to the network. The old code called loadVisiblePlayers()
        // here, which both cost a round trip for data we already knew AND
        // didn't work: it never wrote back into allPlayers, which is the
        // cache the picker and the banner actually read from. That's why
        // a saved avatar/banner appeared to do nothing until you
        // navigated away and came back.
        const myId = myPlayerId();
        const idx = allPlayers.findIndex(p => p.player_id === myId);
        // Mirror the server's mutual-exclusion locally so the card
        // re-renders correctly without a refetch: setting a preset clears
        // the paired upload, and setting an upload clears the paired
        // preset. Otherwise the stale one lingers in the cache and the
        // render picks the wrong layer.
        const patch = { [field]: value };
        if (field === 'avatar_id') patch.avatar_url = null;
        if (field === 'banner_id') patch.banner_url = null;
        if (field === 'background_id') patch.background_url = null;
        if (field === 'avatar_url' && value) patch.avatar_id = null;
        if (field === 'banner_url' && value) patch.banner_id = null;
        if (field === 'background_url' && value) patch.background_id = null;
        // The server also prepends a new upload to its kept-list, but the
        // local cache didn't mirror that - so the "your uploads" strip
        // stayed empty until a full reload. Mirror it here: when a *_url is
        // set to a real key, move it to the front of the matching *_uploads
        // (deduped, capped at 3) exactly as the backend rotation does.
        const uploadKinds = { avatar_url: 'avatar_uploads', banner_url: 'banner_uploads', background_url: 'background_uploads' };
        if (uploadKinds[field] && value && idx >= 0) {
          const listKey = uploadKinds[field];
          const existing = (allPlayers[idx][listKey] || []).filter(k => k && k !== value);
          patch[listKey] = [value, ...existing].slice(0, 3);
        }
        if (idx >= 0) allPlayers[idx] = { ...allPlayers[idx], ...patch };
        const me = allPlayers[idx] || patch;

        // Re-render the upload strips so the just-used image appears/highlights
        // without waiting for the modal to be reopened.
        if (typeof renderUploadStrip === 'function' && me) {
          renderUploadStrip('avatar', me);
          renderUploadStrip('banner', me);
          renderUploadStrip('background', me);
        }

        renderSettingsPickers(me);  // highlight the newly-selected preset
        if (document.getElementById('profile_player_select').value === myId) {
          renderProfileCardBanner(me);  // instant, no fetch
        }
        // Settings can be opened from any tab, so the page background has
        // to update even when the Player Card isn't the active one.
        updatePageBackground();
      } catch (err) {
        statusEl.textContent = `Request failed: ${err.message}`;
      }
    }

    async function loadProfileBundle(playerId) {
      try {
        const res = await fetch(`${API_BASE_URL}/profile-secure/matches?profile_bundle_for=${playerId}`, { headers: getAuthHeaders() });
        const bundle = await res.json();

        // Recent form
        const formEl = document.getElementById('profile-form-result');
        const form = bundle.recent_form && bundle.recent_form.form;
        if (!form || !form.length) {
          formEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No matches recorded yet.</p>';
        } else {
          formEl.innerHTML = form.map(f => {
            const partnerNames = f.partner_names || [];
            const partnerText = partnerNames.length ? ` with ${partnerNames.join(' & ')}` : '';
            const detail = `${f.result === 'W' ? 'Won' : 'Lost'}${partnerText} vs ${(f.opponent_names || []).join(' & ')} on ${f.date ? f.date.slice(0, 10) : ''}`;
            const detailEscaped = detail.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            // title= gives desktop hover for free; onclick gives mobile/tablet
            // the SAME info via tap, since touch devices have no hover state
            // at all - relying on title alone left this silently broken there.
            const delta = Math.round(Number(f.delta) || 0);
            const sign = delta > 0 ? '+' : '';
            // Chip keeps its existing win/loss background color; the label
            // inside is now the rating change instead of the W/L letter.
            return `<div class="form-chip ${f.result === 'W' ? 'win' : 'loss'}" title="${detailEscaped}" onclick="nwAlert('${detailEscaped}')">${sign}${delta}</div>`;
          }).join('');
        }

        // Attendance
        const attEl = document.getElementById('profile-attendance-result');
        const attRow = (bundle.attendance || []).find(a => a.player_id === playerId);
        if (attRow) {
          attEl.innerHTML = `<p>Sessions: ${attRow.sessions_attended} &middot; Total matches: ${attRow.total_matches} &middot; Last 30d: ${attRow.matches_last_30_days} &middot; Last 90d: ${attRow.matches_last_90_days} &middot; Longest week streak: ${attRow.longest_week_streak}</p>`;
        } else {
          attEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No matches recorded yet.</p>';
        }

        // Overall record
        const recEl = document.getElementById('profile-overall-record-result');
        const rec = bundle.overall_record;
        const totalGames = rec.total_wins + rec.total_losses;
        const totalRate = totalGames ? Math.round(rec.total_wins / totalGames * 100) : 0;
        const segments = [
          { label: 'Singles wins', value: rec.singles_wins, color: 'var(--court)' },
          { label: 'Doubles wins', value: rec.doubles_wins, color: 'var(--rally)' },
          { label: 'Singles losses', value: rec.singles_losses, color: 'var(--shuttle)' },
          { label: 'Doubles losses', value: rec.doubles_losses, color: 'var(--smash)' }
        ];
        const segTotal = segments.reduce((sum, s) => sum + s.value, 0);
        const barHtml = segTotal
          ? segments.map(s => `<div style="width:${s.value / segTotal * 100}%; background:${s.color};"></div>`).join('')
          : '';
        const legendHtml = segments.map(s =>
          `<span><span class="swatch" style="background:${s.color};"></span>${s.label}: ${s.value}</span>`
        ).join('');
        recEl.innerHTML = `
          <p style="margin:0 0 6px;"><strong>Overall: ${rec.total_wins}-${rec.total_losses}</strong> (${totalRate}% win rate)</p>
          <div class="overall-record-bar">${barHtml}</div>
          <div class="overall-record-legend">${legendHtml}</div>
        `;

        // Top opponents (with an auto-computed rivalry callout on top)
        const oppEl = document.getElementById('profile-top-opponents-result');
        const opponents = bundle.top_opponents && bundle.top_opponents.opponents;
        if (!opponents || !opponents.length) {
          oppEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No opponents recorded yet.</p>';
        } else {
          // Nemesis = worst win rate against, favourite = best, both min 3
          // meetings so a single loss doesn't crown anyone. Ties broken by
          // more meetings (a 1-5 rivalry stings more than 0-3).
          const qualified = opponents.filter(o => o.matches >= 3)
            .map(o => ({ ...o, pct: o.wins / o.matches }));
          let callout = '';
          if (qualified.length) {
            const nemesis = [...qualified].sort((a, b) => a.pct - b.pct || b.matches - a.matches)[0];
            const favourite = [...qualified].sort((a, b) => b.pct - a.pct || b.matches - a.matches)[0];
            if (nemesis.pct < 0.5) {
              callout += `<p style="font-size:13px;">😤 <strong>Nemesis:</strong> ${nemesis.opponent_name} - you're ${nemesis.wins}-${nemesis.losses} against them</p>`;
            }
            if (favourite.pct > 0.5 && favourite.opponent_id !== nemesis.opponent_id) {
              callout += `<p style="font-size:13px;">😎 <strong>Favourite opponent:</strong> ${favourite.opponent_name} - ${favourite.wins}-${favourite.losses} in your favour</p>`;
            }
            if (!callout) {
              callout = '<p style="font-size:13px;">⚖️ No clear nemesis or favourite yet - your rivalries are all evenly matched.</p>';
            }
          }
          oppEl.innerHTML = callout + opponents.map(o => {
            const winPct = o.matches ? (o.wins / o.matches * 100) : 0;
            const lossPct = 100 - winPct;
            return `
              <div class="h2h-row">
                <div class="h2h-row-header">
                  <span>${o.opponent_name}</span>
                  <span>${o.wins}-${o.losses} (${o.matches} match${o.matches === 1 ? '' : 'es'})</span>
                </div>
                <div class="h2h-bar">
                  <div class="h2h-bar-wins" style="width:${winPct}%;"></div>
                  <div class="h2h-bar-losses" style="width:${lossPct}%;"></div>
                </div>
              </div>`;
          }).join('');
        }

        // Achievements (streak line + tiered/binary cards)
        const hof = bundle.hall_of_fame;
        const badges = bundle.progress_badges;
        const milestones = bundle.achievements;

        const streakLineEl = document.getElementById('profile-streak-line');
        const currentStreak = milestones.current_streak || 0;
        streakLineEl.textContent = currentStreak > 0
          ? `Current streak: ${currentStreak} win${currentStreak === 1 ? '' : 's'} in a row`
          : 'No active win streak right now';

        const cards = [];

        function renderTieredCard(icon, name, unit, tiers, currentValue) {
          let tierIndex = -1;
          for (let i = 0; i < tiers.length; i++) {
            if (currentValue >= tiers[i]) tierIndex = i;
          }
          const achieved = tierIndex >= 0;
          const nextTarget = tierIndex + 1 < tiers.length ? tiers[tierIndex + 1] : null;
          // Bar and label must speak the same language: the label reads
          // "currentValue/nextTarget" (absolute), so the bar fills to the
          // same fraction. Previously it measured progress only within the
          // prev->next segment, so completing a tier reset the bar to 0%
          // while the label still implied partial progress (e.g. "3/5").
          const progressPct = nextTarget ? Math.min(100, (currentValue / nextTarget) * 100) : 100;

          let html = `<div class="achievement-card ${achieved ? 'achieved' : 'locked'}">`;
          html += `<div class="achievement-icon">${icon}</div>`;
          html += `<div class="achievement-name">${name}${achieved ? ` &middot; Tier ${tierIndex + 1}/${tiers.length}` : ''}</div>`;
          html += `<div class="achievement-desc">${unit}</div>`;
          if (nextTarget !== null) {
            html += `<div class="achievement-progress-track"><div class="achievement-progress-fill" style="width:${progressPct}%;"></div></div>`;
            html += `<div class="achievement-progress-text">${currentValue}/${nextTarget}</div>`;
          } else {
            html += `<div class="achievement-progress-text">Max tier reached (${currentValue})</div>`;
          }
          html += `</div>`;
          cards.push({ html, sortKey: achieved ? (10 + tierIndex) : 0 });
        }

        function renderBinaryCard(icon, name, desc, achieved, detail) {
          const titleAttr = detail ? ` title="${String(detail).replace(/"/g, '&quot;')}"` : '';
          let html = `<div class="achievement-card ${achieved ? 'achieved' : 'locked'}"${titleAttr}>`;
          html += `<div class="achievement-icon">${icon}</div>`;
          html += `<div class="achievement-name">${name}</div>`;
          html += `<div class="achievement-desc">${desc}</div>`;
          if (achieved && detail) {
            html += `<div class="achievement-progress-text" style="margin-top:6px; opacity:0.9;">${detail}</div>`;
          }
          html += `</div>`;
          cards.push({ html, sortKey: achieved ? 5 : -1 });
        }

        renderTieredCard('🎮', 'Court Regular', 'matches played', [1, 10, 50, 100, 250, 500, 1000], milestones.total_matches || 0);
        renderTieredCard('🏆', 'Conqueror', 'tournament wins', [1, 5, 10, 25], milestones.tournament_wins || 0);
        renderTieredCard('🔥', 'On Fire', 'best personal win streak', [3, 5, 10, 15], milestones.personal_best_streak || 0);
        renderTieredCard('🏅', 'Podium', 'tournament podium finishes', [1, 3, 5, 10], milestones.podium_finishes || 0);
        renderTieredCard('🎲', 'Deuce Demon', 'wins by 2 after deuce', [1, 5, 15, 30], milestones.deuce_wins || 0);
        renderTieredCard('🛡️', 'Iron Day', 'undefeated sessions (3+ matches)', [1, 3, 5, 10], milestones.undefeated_sessions || 0);
        renderTieredCard('📅', 'Ever-Present', 'best attendance streak (sessions)', [3, 5, 10, 20], milestones.best_attendance_streak || 0);
        renderTieredCard('⛰️', 'Summit', 'peak rating reached', [1050, 1100, 1150, 1200, 1300, 1400, 1500, 1700, 2000], milestones.peak_rating || 0);
        renderTieredCard('✅', 'Winner', 'total matches won', [10, 50, 100, 250, 500], milestones.total_wins || 0);
        renderTieredCard('🥈', 'Finalist', 'tournament finals reached', [1, 3, 5, 10], (milestones.tournament_wins || 0) + (milestones.runner_ups || 0));
        // Grit / consolation - reward showing up and battling, win or lose.
        renderTieredCard('🧱', 'Battle-Hardened', 'matches played through defeat (total losses)', [10, 50, 100, 250], milestones.total_losses || 0);
        renderTieredCard('💗', 'Never Say Die', 'kept playing through a losing streak', [3, 5, 8, 12], milestones.worst_loss_streak || 0);

        renderBinaryCard('🥇', 'Longest win streak', 'Overall record holder', hof.longest_win_streak && hof.longest_win_streak.player_id === playerId);
        renderBinaryCard('📈', 'Peak performer', 'Highest peak rating ever', hof.peak_ratings && hof.peak_ratings[0] && hof.peak_ratings[0].player_id === playerId);
        renderBinaryCard('🎯', 'Most consistent', 'Lowest rating volatility', hof.most_consistent && hof.most_consistent[0] && hof.most_consistent[0].player_id === playerId);
        const giantKiller = hof.giant_killer_top5 && hof.giant_killer_top5[0];
        const giantKillerAchieved = giantKiller && giantKiller.winner_ids && giantKiller.winner_ids.includes(playerId);
        renderBinaryCard('💥', 'Giant killer', 'Biggest upset on record', giantKillerAchieved,
          giantKillerAchieved ? `vs ${giantKiller.loser_names.join(' & ')}, ${giantKiller.score}, ${(giantKiller.date || '').slice(0, 10)}` : null);

        const comeback = hof.comeback_top5 && hof.comeback_top5[0];
        const comebackAchieved = comeback && comeback.winner_ids && comeback.winner_ids.includes(playerId);
        renderBinaryCard('🔄', 'Comeback king', 'Biggest comeback on record', comebackAchieved,
          comebackAchieved ? `overcame a ${comeback.deficit_overcome}-pt deficit, ${(comeback.date || '').slice(0, 10)}` : null);

        const blowout = hof.biggest_blowout;
        const blowoutTeamAWon = blowout && blowout.score_a > blowout.score_b;
        const blowoutWinningIds = blowout ? (blowoutTeamAWon ? (blowout.team_a_ids || []) : (blowout.team_b_ids || [])) : [];
        const blowoutAchieved = blowout && blowoutWinningIds.includes(playerId);
        const blowoutOpponentNames = blowout ? (blowoutTeamAWon ? blowout.team_b_names : blowout.team_a_names) : [];
        renderBinaryCard('💪', 'Blowout winner', 'Biggest margin on record', blowoutAchieved,
          blowoutAchieved ? `vs ${(blowoutOpponentNames || []).join(' & ')}, margin ${blowout.margin}, ${(blowout.date || '').slice(0, 10)}` : null);
        renderBinaryCard('🎾', 'Format specialist', 'Biggest singles/doubles gap', hof.format_specialists && hof.format_specialists[0] && hof.format_specialists[0].player_id === playerId);
        renderBinaryCard('🚀', 'Deep run master', 'Best knockout-reach rate', hof.deep_run_rates && hof.deep_run_rates[0] && hof.deep_run_rates[0].player_id === playerId);

        const improvedAnyPeriod = ['week', 'month', 'year'].some(p => badges[p] && badges[p].most_improved_top5 && badges[p].most_improved_top5[0] && badges[p].most_improved_top5[0].player_id === playerId && badges[p].most_improved_top5[0].delta > 0);
        renderBinaryCard('🌟', 'Rising star', 'Most improved this week/month/year', improvedAnyPeriod);
        const activeAnyPeriod = ['week', 'month', 'year'].some(p => badges[p] && badges[p].most_active && badges[p].most_active.player_id === playerId);
        renderBinaryCard('⚡', 'Most active', 'Most matches this week/month/year', activeAnyPeriod);

        cards.sort((a, b) => b.sortKey - a.sortKey);
        document.getElementById('profile-achievements-result').innerHTML = cards.map(c => c.html).join('');
      } catch (err) {
        ['profile-form-result', 'profile-attendance-result', 'profile-overall-record-result',
         'profile-top-opponents-result', 'profile-achievements-result'].forEach(id => {
          document.getElementById(id).textContent = `Request failed: ${err.message}`;
        });
      }
    }

    function resetRatingZoom() {
      if (profileRatingChart && typeof profileRatingChart.resetZoom === 'function') {
        profileRatingChart.resetZoom();
      }
    }

    async function loadProfileRatingChart(playerId) {
      try {
        const playerIds = [
          playerId,
          document.getElementById('profile_compare2_select').value,
          document.getElementById('profile_compare3_select').value,
          document.getElementById('profile_compare4_select').value
        ].filter(Boolean);
        const xAxisMode = document.getElementById('profile_xaxis_mode').value;
        const colors = ['#1F7A4D', '#FF4757', '#00B4D8', '#FFD23F'];

        const historyResults = await Promise.all(playerIds.map(pid => fetchRatingHistory(pid)));
        const datasets = historyResults.map((points, i) => {
          const pid = playerIds[i];
          const player = allPlayers.find(p => p.player_id === pid);
          const chartPoints = xAxisMode === 'sequence'
            ? points.map((p, idx) => ({ x: idx + 1, y: p.y }))
            : points.map(p => ({ x: p.x, y: p.y }));
          return {
            label: player ? player.name : pid,
            data: chartPoints,
            borderColor: colors[i % colors.length],
            backgroundColor: colors[i % colors.length],
            tension: 0.15
          };
        });

        if (profileRatingChart) profileRatingChart.destroy();
        const ctx = document.getElementById('profile-rating-canvas').getContext('2d');
        const xScale = xAxisMode === 'sequence'
          ? { type: 'linear', title: { display: true, text: 'Match #' }, ticks: { color: '#888', precision: 0 } }
          : { type: 'time', time: { unit: 'day' }, ticks: { color: '#888' } };

        // Register the zoom plugin once (guarded so re-rendering the chart
        // doesn't double-register). Lets you pinch (touch) / wheel (desktop)
        // to zoom into the time axis and drag to pan - inside the plot, not
        // a browser page zoom. Falls back gracefully if the CDN script didn't
        // load (older cached index.html).
        if (window.ChartZoom && !Chart.registry.plugins.get('zoom')) {
          Chart.register(window.ChartZoom);
        }
        profileRatingChart = new Chart(ctx, {
          type: 'line',
          data: { datasets },
          options: {
            scales: {
              x: xScale,
              y: { ticks: { color: '#888' } }
            },
            plugins: {
              legend: { labels: { color: getComputedStyle(document.body).color } },
              zoom: {
                // Pan with SHIFT+drag (desktop) so plain drag is free for
                // box-zoom; one/two-finger drag pans on touch.
                pan: { enabled: true, mode: 'x', modifierKey: 'shift' },
                zoom: {
                  // CTRL+wheel zooms so a plain scroll still scrolls the page
                  // (plain wheel-zoom was hijacking normal scrolling).
                  wheel: { enabled: true, modifierKey: 'ctrl' },
                  pinch: { enabled: true },                       // mobile
                  drag: { enabled: true, backgroundColor: 'rgba(47,169,104,0.15)' },  // drag a box to zoom (desktop)
                  mode: 'x'
                },
                // Clamp pan/zoom to the data's own extent so you can never
                // zoom or pan OUT past the data (which collapsed everything to
                // a flat line with no way back but a refresh). minRange is the
                // furthest you can zoom IN, and is unit-correct per axis mode:
                // match-count in 'sequence' mode, milliseconds in 'time' mode.
                limits: {
                  x: {
                    min: 'original',
                    max: 'original',
                    minRange: xAxisMode === 'sequence' ? 5 : 2 * 24 * 60 * 60 * 1000
                  }
                }
              }
            }
          }
        });
        const resetBtn = document.getElementById('rating-chart-reset');
        if (resetBtn) resetBtn.style.display = 'inline-block';
      } catch (err) {
        console.error(err);
      }
    }

    async function loadProfilePartnershipsAndRadar(playerId) {
      const partnershipsGroupId = document.getElementById('profile_partnerships_scope_group').value;
      const partnershipsTournamentFilter = document.getElementById('profile_partnerships_tournament_filter').value;
      const partnershipsTopN = document.getElementById('profile_partnerships_top_n').value;
      const partnershipsHighlight = document.getElementById('profile_partnerships_highlight_tournament').checked;
      const baseParams = new URLSearchParams();
      if (partnershipsGroupId) baseParams.set('group_id', partnershipsGroupId);
      baseParams.set('tournament_filter', partnershipsTournamentFilter);

      const tableParams = new URLSearchParams(baseParams);
      tableParams.set('partnerships_for', playerId);
      const radarParams = new URLSearchParams(baseParams);
      radarParams.set('radar_for', playerId);
      radarParams.set('top_n', partnershipsTopN);

      const [tableResult, radarResult] = await Promise.allSettled([
        fetch(`${API_BASE_URL}/profile-secure/matches?${tableParams.toString()}`, { headers: getAuthHeaders() }).then(r => r.json()),
        fetch(`${API_BASE_URL}/profile-secure/matches?${radarParams.toString()}`, { headers: getAuthHeaders() }).then(r => r.json())
      ]);

      const el = document.getElementById('profile-partnerships-result');
      if (tableResult.status === 'fulfilled') {
        const data = tableResult.value;
        if (!data.partnerships.length) {
          el.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No doubles matches in this scope yet.</p>';
        } else {
          let html = '<table><tr><th>Partner</th><th>Matches</th><th>W</th><th>L</th><th>Win %</th></tr>';
          data.partnerships.forEach(p => {
            html += `<tr><td>${p.partner_name}</td><td>${p.matches}</td><td>${p.wins}</td><td>${p.losses}</td><td>${p.win_rate}%</td></tr>`;
          });
          html += '</table>';
          el.innerHTML = html;
        }
      } else {
        el.textContent = `Request failed: ${tableResult.reason.message}`;
      }

      if (radarResult.status === 'fulfilled') {
        const data = radarResult.value;
        if (data.partners && data.partners.length) {
          renderPartnerRadar(data, partnershipsHighlight, 'profile-radar-svg');
        } else {
          document.getElementById('profile-radar-svg').innerHTML = '';
        }
      } else {
        console.error(radarResult.reason);
      }
    }

    async function loadProfileHeadToHead(playerId) {
      const opponentId = document.getElementById('profile_h2h_opponent_select').value;
      const h2hEl = document.getElementById('profile-h2h-result');
      if (!opponentId) {
        h2hEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">Pick someone to compare against.</p>';
        return;
      }
      if (opponentId === playerId) {
        h2hEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">Pick a different player to compare against.</p>';
        return;
      }
      try {
        const res = await fetch(`${API_BASE_URL}/profile-secure/matches?head_to_head=${playerId}&opponent=${opponentId}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!data.matches) {
          h2hEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">These two have never played against each other.</p>';
        } else {
          h2hEl.innerHTML = `<p><strong>${data.wins}-${data.losses}</strong> (${data.win_rate}% win rate) across ${data.matches} match${data.matches === 1 ? '' : 'es'}</p>`;
        }
      } catch (err) {
        h2hEl.textContent = `Request failed: ${err.message}`;
      }
    }

    async function loadProfileWithPartner(playerId) {
      const partnerId = document.getElementById('profile_partner_select').value;
      const el = document.getElementById('profile-partner-result');
      if (!partnerId) {
        el.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">Pick a partner to see your record together.</p>';
        return;
      }
      if (partnerId === playerId) {
        el.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">Pick a different player as the partner.</p>';
        return;
      }
      try {
        const res = await fetch(`${API_BASE_URL}/profile-secure/matches?with_partner=${playerId}&partner=${partnerId}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!data.matches) {
          el.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">These two have never played on the same side.</p>';
        } else {
          partnerGames = data.games || [];
          partnerPage = 0;
          const summary = `<p><strong>${data.wins}-${data.losses}</strong> (${data.win_rate}% win rate) across ${data.matches} match${data.matches === 1 ? '' : 'es'} as partners</p>`;
          el.innerHTML = summary + '<div id="profile-partner-games"></div>';
          renderPartnerGames();
        }
      } catch (err) {
        el.textContent = `Request failed: ${err.message}`;
      }
    }

    let partnerGames = [];
    let partnerPage = 0;
    const PARTNER_PAGE_SIZE = 25;

    function partnerGamesGoto(p) { partnerPage = p; renderPartnerGames(); }

    function renderPartnerGames() {
      const wrap = document.getElementById('profile-partner-games');
      if (!wrap) return;
      const total = partnerGames.length;
      if (!total) { wrap.innerHTML = ''; return; }
      const pages = Math.max(1, Math.ceil(total / PARTNER_PAGE_SIZE));
      if (partnerPage >= pages) partnerPage = pages - 1;
      if (partnerPage < 0) partnerPage = 0;
      const start = partnerPage * PARTNER_PAGE_SIZE;
      const pageRows = partnerGames.slice(start, start + PARTNER_PAGE_SIZE);

      let html = '<table><tr><th>Date</th><th>Opponents</th><th>Score</th><th>Result</th></tr>';
      pageRows.forEach(g => {
        const date = g.date ? new Date(g.date).toLocaleDateString() : '';
        const opps = (g.opponents || []).join(' & ') || '-';
        const result = g.won
          ? '<span style="color:var(--court,#2fa968);font-weight:600;">Won</span>'
          : '<span style="color:#c0392b;font-weight:600;">Lost</span>';
        html += `<tr><td>${date}</td><td>${opps}</td><td>${g.our_score} - ${g.their_score}</td><td>${result}</td></tr>`;
      });
      html += '</table>';

      if (pages > 1) {
        const from = start + 1, to = Math.min(start + PARTNER_PAGE_SIZE, total);
        html += `<div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-size:13px;">`
              + `<button class="secondary" style="margin:0;padding:5px 12px;" ${partnerPage === 0 ? 'disabled' : ''} onclick="partnerGamesGoto(${partnerPage - 1})">Prev</button>`
              + `<span style="color:var(--text-secondary);">${from}-${to} of ${total} &middot; page ${partnerPage + 1}/${pages}</span>`
              + `<button class="secondary" style="margin:0;padding:5px 12px;" ${partnerPage >= pages - 1 ? 'disabled' : ''} onclick="partnerGamesGoto(${partnerPage + 1})">Next</button>`
              + `</div>`;
      }
      wrap.innerHTML = html;
    }

    function skeletonHTML(lines = 3) {
      const widths = ['70%', '90%', '55%', '80%', '65%'];
      return Array.from({ length: lines }, (_, i) =>
        `<div class="skeleton-line" style="width:${widths[i % widths.length]};"></div>`
      ).join('');
    }

    function showProfileSkeletons() {
      document.getElementById('profile-form-result').innerHTML = skeletonHTML(1);
      document.getElementById('profile-attendance-result').innerHTML = skeletonHTML(1);
      document.getElementById('profile-overall-record-result').innerHTML = skeletonHTML(3);
      document.getElementById('profile-top-opponents-result').innerHTML = skeletonHTML(5);
      document.getElementById('profile-achievements-result').innerHTML = skeletonHTML(3);
      document.getElementById('profile-streak-line').textContent = '';
      document.getElementById('profile-partnerships-result').innerHTML = skeletonHTML(3);
      document.getElementById('profile-radar-svg').innerHTML = '';
      document.getElementById('profile-h2h-result').innerHTML = skeletonHTML(1);
      if (profileRatingChart) { profileRatingChart.destroy(); profileRatingChart = null; }
    }

    function renderXpPanel(player) {
      const inline = document.getElementById('profile-xp-inline');
      const bannerLevel = document.getElementById('profile-banner-level');
      if (!player) { if (inline) inline.style.display = 'none'; if (bannerLevel) bannerLevel.style.display = 'none'; return; }

      const xp = Number(player.xp || 0);
      const COEFF = 5;
      const level = Math.max(1, Math.floor(Math.sqrt(Math.floor(xp / COEFF))));
      const xpForLevel = (n) => COEFF * n * n;
      const curFloor = xpForLevel(level);
      const nextFloor = xpForLevel(level + 1);
      const into = Math.max(0, xp - curFloor);
      const span = Math.max(1, nextFloor - curFloor);
      const pct = Math.min(100, Math.round((into / span) * 100));

      // Level under the banner name.
      if (bannerLevel) {
        bannerLevel.textContent = `Level ${level}`;
        bannerLevel.style.display = xpVisible() ? 'block' : 'none';
      }

      // The progress bar now sits inside the Player Card, right under the
      // banner - admin-only during soft launch.
      if (!inline) return;
      if (!xpVisible()) { inline.style.display = 'none'; return; }
      inline.style.display = 'block';
      document.getElementById('profile-xp-current').textContent = `${xp.toLocaleString()} XP`;
      document.getElementById('profile-xp-next').textContent = `${(nextFloor - xp).toLocaleString()} to level ${level + 1}`;
      document.getElementById('profile-xp-bar').style.width = `${pct}%`;
    }

    /** The coin chip in the header shows ONLY the logged-in user's own
     *  balance - never anyone else's, even when viewing their card. Private
     *  by design. Admin-gated during soft launch like the rest of the XP UI. */
    function updateHeaderCoins() {
      const chip = document.getElementById('header-coins');
      if (!chip) return;
      const me = allPlayers.find(p => p.player_id === myPlayerId());
      if (me && xpVisible()) {
        document.getElementById('header-coins-num').textContent = Number(me.coins || 0).toLocaleString();
        chip.style.display = 'inline-block';
      } else {
        chip.style.display = 'none';
      }
    }

    async function loadProfile() {
      const playerId = document.getElementById('profile_player_select').value;
      if (!playerId) return;

      const player = allPlayers.find(p => p.player_id === playerId);
      renderProfileCardBanner(player);
      renderXpPanel(player);

      showProfileSkeletons();

      // All four sections are independent of each other - run them
      // concurrently instead of one-after-another, so total load time is
      // bounded by the slowest single request rather than the sum of all.
      await Promise.allSettled([
        loadProfileBundle(playerId),
        loadProfileRatingChart(playerId),
        loadProfilePartnershipsAndRadar(playerId),
        loadProfileHeadToHead(playerId),
        loadProfileWithPartner(playerId)
      ]);
    }
    /** Manual reload of whoever is currently selected. Before this, the
     *  only way to see updated numbers was to select a different player
     *  and then select yourself again - loadProfile() only ever fires on
     *  a `change` event, and re-picking the same option is not a change. */
    async function refreshProfile() {
      const btn = document.getElementById('profile-refresh-btn');
      const notice = document.getElementById('profile-stale-notice');
      btn.disabled = true;
      btn.style.opacity = '0.5';
      try {
        await loadVisiblePlayers({ keepSelection: true });  // picks up rating/avatar/banner changes
        await loadProfile();
        notice.style.display = 'none';
      } finally {
        btn.disabled = false;
        btn.style.opacity = '1';
      }
    }

    /** Called after a match is saved: if the card on screen belongs to
     *  someone who just played, quietly reload it. */
    function refreshProfileIfShowing(affectedPlayerIds) {
      const selected = document.getElementById('profile_player_select').value;
      if (!selected) return;
      if (affectedPlayerIds.includes(selected)) {
        refreshProfile();
      } else {
        // Someone else's card is up, but the rankings behind it moved -
        // flag it rather than firing a fetch nobody asked for.
        document.getElementById('profile-stale-notice').style.display = 'block';
      }
    }

    document.getElementById('profile_player_select').addEventListener('change', loadProfile);
    document.getElementById('profile_h2h_opponent_select').addEventListener('change', loadProfile);
    document.getElementById('profile_partner_select').addEventListener('change', () => loadProfileWithPartner(document.getElementById('profile_player_select').value));
    ['profile_compare2_select', 'profile_compare3_select', 'profile_compare4_select', 'profile_xaxis_mode',
     'profile_partnerships_scope_group', 'profile_partnerships_tournament_filter',
     'profile_partnerships_top_n', 'profile_partnerships_highlight_tournament'].forEach(id => {
      document.getElementById(id).addEventListener('change', loadProfile);
    });

    function renderPartnerRadar(data, highlightTournament, svgId = 'radar-svg') {
      const svg = document.getElementById(svgId);
      const size = 600, center = size / 2, maxRadius = size / 2 - 90;
      const n = data.partners.length;

      const spokeAngles = data.partners.map((p, i) => (2 * Math.PI * i) / n - Math.PI / 2);

      let svgContent = '';
      spokeAngles.forEach(angle => {
        const x = center + maxRadius * Math.cos(angle);
        const y = center + maxRadius * Math.sin(angle);
        svgContent += `<line x1="${center}" y1="${center}" x2="${x}" y2="${y}" stroke="var(--border)" stroke-width="1" />`;
      });

      const points = data.partners.map((p, i) => {
        const angle = spokeAngles[i];
        const r = (p.percentage / 100) * maxRadius;
        return { x: center + r * Math.cos(angle), y: center + r * Math.sin(angle) };
      });
      const polygonPoints = points.map(pt => `${pt.x},${pt.y}`).join(' ');
      svgContent += `<polygon points="${polygonPoints}" fill="var(--court)" fill-opacity="0.3" stroke="var(--court)" stroke-width="1.5" />`;

      if (highlightTournament) {
        const nonTournamentPoints = data.partners.map((p, i) => {
          const angle = spokeAngles[i];
          const nonTournamentPct = p.percentage - p.tournament_percentage;
          const r = (nonTournamentPct / 100) * maxRadius;
          return { x: center + r * Math.cos(angle), y: center + r * Math.sin(angle) };
        });
        const nonTournamentPolygon = nonTournamentPoints.map(pt => `${pt.x},${pt.y}`).join(' ');
        svgContent += `<polygon points="${nonTournamentPolygon}" fill="none" stroke="var(--shuttle)" stroke-width="2" stroke-dasharray="6 4" />`;
      }

      data.partners.forEach((p, i) => {
        const angle = spokeAngles[i];
        const labelX = center + (maxRadius + 30) * Math.cos(angle);
        const labelY = center + (maxRadius + 30) * Math.sin(angle);
        const pctLabel = highlightTournament && p.tournament_matches > 0
          ? `${p.percentage}% (${p.tournament_percentage}% from tournaments)`
          : `${p.percentage}%`;
        svgContent += `<text x="${labelX}" y="${labelY - 6}" font-size="13" fill="var(--text)" text-anchor="middle">${p.name}</text>`;
        svgContent += `<text x="${labelX}" y="${labelY + 10}" font-size="11" fill="var(--text-secondary)" text-anchor="middle">${pctLabel}</text>`;
      });

      if (highlightTournament) {
        svgContent += `<text x="${center}" y="${size - 20}" font-size="11" fill="var(--text-secondary)" text-anchor="middle">Solid = overall share. Dashed = share from standalone (non-tournament) matches only.</text>`;
      }

      svgContent += `<circle cx="${center}" cy="${center}" r="3" fill="var(--text)" />`;
      svgContent += `<text x="${center}" y="${center - 10}" font-size="11" fill="var(--text-secondary)" text-anchor="middle">${data.total_matches} matches</text>`;

      svg.innerHTML = svgContent;
    }

    let lastHistoryData = null;

    async function loadHistory() {
      const scope = document.getElementById('history_scope_select').value;
      const period = document.getElementById('history_period_select').value;
      const resultEl = document.getElementById('history-result');
      resultEl.textContent = 'Loading...';

      try {
        const params = new URLSearchParams({ progress_history: 'true', scope, period });
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
        lastHistoryData = data;
        renderHistory(data);
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    function renderHistory(data) {
      const resultEl = document.getElementById('history-result');
      try {
        let html = '';
        const streaks = (data.current_streaks && data.current_streaks.length)
          ? data.current_streaks
          : (data.current_streak ? [data.current_streak] : []);
        if (streaks.length) {
          // Co-winners can be on different streak lengths (A & B tie this
          // week, but only A also won last week) - group by streak length.
          const byLen = {};
          streaks.forEach(s => { (byLen[s.streak] = byLen[s.streak] || []).push(playerLabelById(s.player_id)); });
          const parts = Object.keys(byLen).sort((a, b) => b - a)
            .map(len => `${byLen[len].join(' & ')} - ${len} period${len === '1' ? '' : 's'} in a row`);
          html += `<p>🔥 <strong>Current streak:</strong> ${parts.join('; ')}</p>`;
        }
        if (data.longest_streaks && data.longest_streaks.length) {
          html += '<h4>Longest streaks ever</h4><table><tr><th>Player</th><th>Streak</th></tr>';
          data.longest_streaks.slice(0, 5).forEach(s => {
            html += `<tr><td>${playerLabelById(s.player_id)}</td><td>${s.streak}</td></tr>`;
          });
          html += '</table>';
        }
        if (data.holder_counts && data.holder_counts.length) {
          html += '<h4>Times held "most improved"</h4><table><tr><th>Player</th><th>Times</th></tr>';
          data.holder_counts.slice(0, 5).forEach(h => {
            html += `<tr><td>${playerLabelById(h.player_id)}</td><td>${h.count}</td></tr>`;
          });
          html += '</table>';
        }
        if (data.history && data.history.length) {
          html += '<h4>Full history</h4><table><tr><th>Period start</th><th>Most improved</th><th>Most active</th></tr>';
          [...data.history].reverse().forEach(h => {
            // Prefer the co-winner list; fall back to the legacy singular
            // field for rows written before ties were recorded properly.
            const improvedName = (h.most_improved_player_ids && h.most_improved_player_ids.length)
              ? playerLabelsById(h.most_improved_player_ids, h.most_improved_names).join(' & ')
              : ((h.most_improved_names && h.most_improved_names.length) ? h.most_improved_names.join(' & ') : h.most_improved_name);
            const activeName = (h.most_active_player_ids && h.most_active_player_ids.length)
              ? playerLabelsById(h.most_active_player_ids, h.most_active_names).join(' & ')
              : ((h.most_active_names && h.most_active_names.length) ? h.most_active_names.join(' & ') : h.most_active_name);
            const improved = improvedName ? `${improvedName} (${h.most_improved_delta >= 0 ? '+' : ''}${h.most_improved_delta})` : '-';
            const active = activeName ? `${activeName} (${h.most_active_matches})` : '-';
            const computedTip = h.computed_at ? ` title="Locked in: ${h.computed_at.slice(0, 19).replace('T', ' ')} UTC"` : '';
            html += `<tr${computedTip}><td>${h.period_start}</td><td>${improved}</td><td>${active}</td></tr>`;
          });
          html += '</table>';
        } else {
          html = '<p style="font-size:13px;color:var(--text-secondary);">No completed periods recorded yet for this scope.</p>';
        }
        resultEl.innerHTML = html;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }
    document.getElementById('load-history-btn').addEventListener('click', loadHistory);
    document.getElementById('history_scope_select').addEventListener('change', loadHistory);
    document.getElementById('history_period_select').addEventListener('change', loadHistory);

    let lastBadgesData = null;

    async function loadBadges() {
      const groupId = document.getElementById('badges_group_filter').value;
      const resultEl = document.getElementById('badges-result');
      resultEl.textContent = 'Loading...';

      try {
        const params = new URLSearchParams({ progress_badges: 'true' });
        if (groupId) params.set('group_id', groupId);
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
        lastBadgesData = data;
        renderBadges(data);
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    function renderBadges(data) {
      const resultEl = document.getElementById('badges-result');
      try {
        const periodLabels = { week: 'This week', month: 'This month', year: 'This year' };
        let html = '';
        ['week', 'month', 'year'].forEach(period => {
          const p = data[period];
          html += `<h4>${periodLabels[period]}</h4>`;
          if (!p.most_improved_top5.length) {
            html += '<p style="font-size:13px;color:var(--text-secondary);">No matches in this period yet.</p>';
            return;
          }
          const topImprover = p.most_improved_top5[0];
          html += `<p>🔥 <strong>Most improved:</strong> ${playerLabelById(topImprover.player_id, topImprover.name)} (${topImprover.delta >= 0 ? '+' : ''}${topImprover.delta})</p>`;
          if (p.most_active) {
            html += `<p>⚡ <strong>Most active:</strong> ${playerLabelById(p.most_active.player_id, p.most_active.name)} (${p.most_active.matches} match${p.most_active.matches === 1 ? '' : 'es'})</p>`;
          }
          html += '<table><tr><th>Player</th><th>Change</th><th>Current rating</th><th>Matches</th></tr>';
          p.most_improved_top5.forEach(row => {
            html += `<tr><td>${playerLabelById(row.player_id, row.name)}</td><td>${row.delta >= 0 ? '+' : ''}${row.delta}</td><td class="rating">${row.current_rating}</td><td>${row.matches_in_period}</td></tr>`;
          });
          html += '</table>';
        });
        resultEl.innerHTML = html;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }
    document.getElementById('load-badges-btn').addEventListener('click', loadBadges);
    document.getElementById('badges_group_filter').addEventListener('change', loadBadges);

    let lastDiversityData = null;

    async function loadDiversity() {
      const groupId = document.getElementById('diversity_group_filter').value;
      const resultEl = document.getElementById('diversity-result');
      resultEl.textContent = 'Loading...';

      try {
        const params = new URLSearchParams({ diversity: 'true' });
        if (groupId) params.set('group_id', groupId);
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
        lastDiversityData = data;
        renderDiversity(data);
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    function renderDiversity(data) {
      const resultEl = document.getElementById('diversity-result');
      if (!data.players.length) { resultEl.innerHTML = '<p style="font-size:13px;color:var(--text-secondary);">No doubles matches in this scope yet.</p>'; return; }

      let html = '<table><tr><th>Player</th><th>Matches</th><th>Distinct partners</th><th>Top partner</th><th>Top partner %</th></tr>';
      data.players.forEach(p => {
        html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.total_matches}</td><td>${p.distinct_partners}</td><td>${playerLabelById(p.top_partner_id, p.top_partner_name)}</td><td>${p.top_partner_pct}%</td></tr>`;
      });
      html += '</table>';
      resultEl.innerHTML = html;
    }
    document.getElementById('load-diversity-btn').addEventListener('click', loadDiversity);
    document.getElementById('diversity_group_filter').addEventListener('change', loadDiversity);

    // Looks up the CURRENT (toggle-aware) label for a player_id from the
    // already-cached allPlayers list, ignoring whatever name string the
    // backend sent (which is a frozen snapshot from whenever that match/
    // stat was computed, and not toggle-aware itself). Every Hall of Fame
    // section already carries the raw player_id(s) alongside its name(s)
    // in the response - this is what makes toggling it possible with
    // zero new API calls or backend changes.
    function playerLabelById(playerId, fallbackName) {
      const p = allPlayers.find(pl => pl.player_id === playerId);
      return p ? formatPlayerLabel(p.name, p.nickname) : (fallbackName || playerId);
    }
    function playerLabelsById(playerIds, fallbackNames) {
      return (playerIds || []).map((pid, i) => playerLabelById(pid, fallbackNames ? fallbackNames[i] : null));
    }

    let lastHofData = null;

    async function loadHallOfFame() {
      const groupId = document.getElementById('hof_group_filter').value;
      const resultEl = document.getElementById('hof-result');
      resultEl.textContent = 'Loading...';

      try {
        const params = new URLSearchParams({ hall_of_fame: 'true' });
        if (groupId) params.set('group_id', groupId);
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
        lastHofData = data;
        renderHallOfFame(data);
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    // Re-renders already-fetched Hall of Fame data with the current
    // toggle state - called both after a real fetch and directly from
    // the display-mode toggle, which must NOT trigger a new network call
    // just to relabel players using data that's already cached.
    function renderHallOfFame(data) {
      const resultEl = document.getElementById('hof-result');
      try {
        let html = '';
        if (data.longest_win_streak) {
          html += `<p><strong>Longest win streak:</strong> ${playerLabelById(data.longest_win_streak.player_id, data.longest_win_streak.name)} - ${data.longest_win_streak.streak} in a row</p>`;
        }
        if (data.biggest_blowout) {
          const b = data.biggest_blowout;
          html += `<p><strong>Biggest blowout:</strong> ${playerLabelsById(b.team_a_ids, b.team_a_names).join(' & ')} ${b.score_a}-${b.score_b} ${playerLabelsById(b.team_b_ids, b.team_b_names).join(' & ')} (margin ${b.margin})</p>`;
        }
        if (data.peak_ratings && data.peak_ratings.length) {
          html += '<h4>Peak ratings ever</h4><table><tr><th>Player</th><th>Peak</th></tr>';
          data.peak_ratings.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td class="rating">${p.rating}</td></tr>`; });
          html += '</table>';
        }
        if (data.giant_killer_top5 && data.giant_killer_top5.length) {
          html += '<h4>Giant-killer upsets</h4><table><tr><th>Winner</th><th>Beat</th><th>Score</th><th>Rating gap</th></tr>';
          data.giant_killer_top5.forEach(g => {
            html += `<tr><td>${playerLabelsById(g.winner_ids, g.winner_names).join(' & ')}</td><td>${playerLabelsById(g.loser_ids, g.loser_names).join(' & ')}</td><td>${g.score}</td><td>${g.upset_gap}</td></tr>`;
          });
          html += '</table>';
        }
        if (data.comeback_top5 && data.comeback_top5.length) {
          html += '<h4>Best comebacks</h4><p class="card-sub">Only available for matches recorded with live scoring.</p><table><tr><th>Winner</th><th>Deficit overcome</th></tr>';
          data.comeback_top5.forEach(c => { html += `<tr><td>${playerLabelsById(c.winner_ids, c.winner_names).join(' & ')}</td><td>${c.deficit_overcome}</td></tr>`; });
          html += '</table>';
        }
        if (data.most_consistent && data.most_consistent.length) {
          html += '<h4>Most consistent</h4><p class="card-sub">Lowest swing in rating from match to match (needs 3+ matches).</p><table><tr><th>Player</th><th>Matches</th><th>Volatility</th></tr>';
          data.most_consistent.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.matches}</td><td>${p.volatility}</td></tr>`; });
          html += '</table>';
        }
        if (data.most_volatile && data.most_volatile.length) {
          html += '<h4>Most volatile</h4><p class="card-sub">Biggest swings, win or lose - the streaky players.</p><table><tr><th>Player</th><th>Matches</th><th>Volatility</th></tr>';
          data.most_volatile.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.matches}</td><td>${p.volatility}</td></tr>`; });
          html += '</table>';
        }
        if (data.format_specialists && data.format_specialists.length) {
          html += '<h4>Format specialists</h4><p class="card-sub">Biggest gap between singles and doubles win rate (needs 2+ matches in each).</p><table><tr><th>Player</th><th>Singles win %</th><th>Doubles win %</th><th>Stronger in</th></tr>';
          data.format_specialists.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.singles_win_pct}%</td><td>${p.doubles_win_pct}%</td><td>${p.stronger_format}</td></tr>`; });
          html += '</table>';
        }
        if (data.best_partnerships && data.best_partnerships.length) {
          html += '<h4>Best partnerships</h4><p class="card-sub">Doubles pairs by win rate, minimum 3 matches together.</p><table><tr><th>Pair</th><th>W-L</th><th>Win %</th></tr>';
          data.best_partnerships.forEach(p => { html += `<tr><td>${playerLabelsById(p.member_ids, p.names).join(' & ')}</td><td>${p.wins}-${p.losses}</td><td>${p.win_pct}%</td></tr>`; });
          html += '</table>';
        }
        if (data.session_mvps && data.session_mvps.length) {
          html += '<h4>Session MVPs</h4><p class="card-sub">Best total rating gain on each play day.</p><table><tr><th>Date</th><th>MVP</th><th>Rating gained</th></tr>';
          data.session_mvps.forEach(s => { html += `<tr><td>${s.date}</td><td>${playerLabelsById(s.player_ids, s.names).join(' & ')}</td><td>+${s.delta}</td></tr>`; });
          html += '</table>';
        }
        if (data.biggest_swings && data.biggest_swings.length) {
          html += '<h4>Biggest single-match rating swings</h4><table><tr><th>Player</th><th>Swing</th></tr>';
          data.biggest_swings.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>+${p.swing}</td></tr>`; });
          html += '</table>';
        }
        if (data.deuce_specialists && data.deuce_specialists.length) {
          html += '<h4>Deuce specialists</h4><p class="card-sub">Wins by exactly 2 points after deuce - the nerveless ones.</p><table><tr><th>Player</th><th>Deuce wins</th></tr>';
          data.deuce_specialists.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.deuce_wins}</td></tr>`; });
          html += '</table>';
        }
        if (data.undefeated_sessions && data.undefeated_sessions.length) {
          html += '<h4>Undefeated sessions</h4><p class="card-sub">Days with 3+ matches and zero losses.</p><table><tr><th>Player</th><th>Perfect days</th></tr>';
          data.undefeated_sessions.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.sessions}</td></tr>`; });
          html += '</table>';
        }
        if (data.deep_run_rates && data.deep_run_rates.length) {
          html += '<h4>Deep-run rate</h4><p class="card-sub">Of the tournaments a player has entered, how often they reached the knockout stage.</p><table><tr><th>Player</th><th>Tournaments entered</th><th>Reached knockout</th><th>Rate</th></tr>';
          data.deep_run_rates.forEach(p => { html += `<tr><td>${playerLabelById(p.player_id, p.name)}</td><td>${p.tournaments_entered}</td><td>${p.reached_knockout}</td><td>${p.deep_run_rate}%</td></tr>`; });
          html += '</table>';
        }
        if (!html) html = '<p style="font-size:13px;color:var(--text-secondary);">Not enough match history yet.</p>';
        resultEl.innerHTML = html;
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }
    document.getElementById('load-hof-btn').addEventListener('click', loadHallOfFame);
    document.getElementById('hof_group_filter').addEventListener('change', loadHallOfFame);

    let lastAttendanceData = null;

    async function loadAttendance() {
      const groupId = document.getElementById('attendance_group_filter').value;
      const resultEl = document.getElementById('attendance-result');
      resultEl.textContent = 'Loading...';

      const params = new URLSearchParams({ attendance: 'true' });
      if (groupId) params.set('group_id', groupId);

      try {
        const res = await fetch(`${API_BASE_URL}/matches?${params.toString()}`);
        const data = await res.json();
        if (!res.ok) { resultEl.textContent = `Error: ${data.error}`; return; }
        lastAttendanceData = data;
        renderAttendance(data);
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    function renderAttendance(data) {
      const resultEl = document.getElementById('attendance-result');
      if (!data.attendance.length) { resultEl.innerHTML = '<p style="font-size:13px;color:#555;">No matches recorded yet.</p>'; return; }

      let html = '<table><tr><th>Player</th><th>Sessions</th><th>Total Matches</th><th>Last 30d</th><th>Last 90d</th><th>Longest week streak</th></tr>';
      data.attendance.forEach(a => {
        html += `<tr><td>${playerLabelById(a.player_id, a.name)}</td><td>${a.sessions_attended}</td><td>${a.total_matches}</td><td>${a.matches_last_30_days}</td><td>${a.matches_last_90_days}</td><td>${a.longest_week_streak}</td></tr>`;
      });
      html += '</table>';
      resultEl.innerHTML = html;
    }
    document.getElementById('load-attendance-btn').addEventListener('click', loadAttendance);
    document.getElementById('attendance_group_filter').addEventListener('change', loadAttendance);


    // ---------- UPI payment card (public) ----------
    // Fetches the current UPI details from the server, then renders the pay
    // card. Called on load and again after an admin edits the ID in
    // Settings, so the QR reflects the live value without a page reload.
    async function refreshUpiCard() {
      try {
        const res = await fetch(`${financeBaseUrl()}/upi/public`);
        if (res.ok) {
          const d = await res.json();
          UPI_ID = d.upi_id || '';
          if (d.upi_name) UPI_NAME = d.upi_name;
        }
      } catch (_) { /* leave whatever we had */ }
      renderUpiCard();
    }

    function renderUpiCard() {
      const card = document.getElementById('upi-pay-card');
      const idSet = !!UPI_ID && !UPI_ID.includes('REPLACE_ME');
      if (!idSet) { card.style.display = 'none'; return; }
      card.style.display = 'block';

      document.getElementById('upi-id-text').textContent = UPI_ID;
      const payUrl = `upi://pay?pa=${encodeURIComponent(UPI_ID)}&pn=${encodeURIComponent(UPI_NAME)}&cu=INR`;
      document.getElementById('upi-deeplink').href = payUrl;
      const copyBtn = document.getElementById('upi-copy-btn');
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(UPI_ID).then(() => {
          copyBtn.textContent = 'Copied ✓';
          setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
        });
      };

      // QR is generated live from the current ID (CDN lib, image-service
      // fallback), so it always matches whatever's set - no committed image
      // to keep in sync now that the ID is editable.
      const qrEl = document.getElementById('upi-qr');
      qrEl.innerHTML = '';
      const imageServiceFallback = () => {
        const img = document.createElement('img');
        img.width = 180; img.height = 180; img.alt = 'UPI QR';
        img.src = `https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(payUrl)}`;
        img.onerror = () => { qrEl.style.display = 'none'; };
        qrEl.appendChild(img);
      };
      if (window.QRCode) {
        try { new QRCode(qrEl, { text: payUrl, width: 180, height: 180 }); }
        catch (e) { imageServiceFallback(); }
      } else {
        const sc = document.createElement('script');
        sc.src = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';
        sc.onload = () => { try { new QRCode(qrEl, { text: payUrl, width: 180, height: 180 }); } catch (e) { imageServiceFallback(); } };
        sc.onerror = imageServiceFallback;
        document.head.appendChild(sc);
      }
    }
    refreshUpiCard();

    // ---------- finance (view-key gated, now SuperAdmin-enforced when logged in) ----------

    let financeKey = sessionStorage.getItem('nw_finance_key') || '';
    let myFinanceRole = 'none';  // none < view < write < delete
    let xpPublic = false;  // when a SuperAdmin flips it on, the XP/level/coins/store/quests UI shows for everyone
    /** Whether the gamification UI (levels, coins, store, quests) is visible
     *  to the current user: always for admins, and for everyone once the
     *  admin has made it public. */
    function xpVisible() { return isSuperAdmin() || xpPublic; }
    const FIN_LEVEL = { none:0, view:1, write:2, delete:3 };

    /** Hides finance controls above the caller's role. View-only users see
     *  numbers but no add/edit forms; write users see those but no delete
     *  buttons. Marked elements carry data-fin-min="write|delete". */
    function applyFinanceRoleVisibility() {
      const lvl = FIN_LEVEL[myFinanceRole] || 0;
      document.querySelectorAll('[data-fin-min]').forEach(el => {
        const need = FIN_LEVEL[el.getAttribute('data-fin-min')] || 99;
        el.style.display = lvl >= need ? '' : 'none';
      });
      // Delete buttons are rendered fresh each time a finance list loads,
      // so they're matched by class and re-hidden here. Edit is a write
      // action, so it hides for view-only users; delete needs the delete
      // tier. Called again at the end of each list render so newly-drawn
      // rows obey the role too.
      document.querySelectorAll('.fin-edit-exp, .fin-edit').forEach(el => {
        el.style.display = lvl >= FIN_LEVEL.write ? '' : 'none';
      });
      document.querySelectorAll('.fin-del').forEach(el => {
        el.style.display = lvl >= FIN_LEVEL.delete ? '' : 'none';
      });
    }

    let currentFinanceGroupId = null;   // which group's ledger the Finance tab is showing

    function finQS(extra) {
      const p = new URLSearchParams(extra || {});
      p.set('view_key', financeKey);
      if (currentFinanceGroupId) p.set('group_id', currentFinanceGroupId);
      return p.toString();
    }

    // Any logged-in user routes through /finance-secure so their Cognito
    // claims reach the Lambda (that's how per-group access is resolved -
    // owners get their group, members get their per-group role). Only a
    // not-logged-in shared-key holder falls back to the legacy open route.
    function financeBaseUrl() {
      return getAuthHeaders().Authorization ? `${API_BASE_URL}/finance-secure` : `${API_BASE_URL}/finance`;
    }

    async function finPost(path, method, bodyObj) {
      const body = { ...bodyObj, view_key: financeKey };
      if (currentFinanceGroupId) body.group_id = currentFinanceGroupId;
      const res = await fetch(`${financeBaseUrl()}/${path}`, {
        method, headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify(body)
      });
      return { ok: res.ok, data: await res.json() };
    }

    /**
     * Before showing the manual key box, ask the server whether this
     * logged-in user is allowed finance access. If so it returns the view
     * key and we unlock silently - most people never see or type the
     * secret again. The manual box stays as a fallback for anyone off the
     * list who still knows the key.
     */
    // Which groups' finance can this caller see? SuperAdmin: all. Otherwise:
    // groups they own/admin, plus any group where they hold a per-group
    // finance role. Defaults the selector to "Club (default)" (the migrated
    // club-wide ledger) when present.
    // Fill the Add-expense / membership / walk-in slot dropdowns from the
    // SELECTED group's slot list (Stage 4 wired slots into the group but not
    // these forms). Falls back to the historical default if a group has none.
    function populateFinanceSlots(group) {
      const slots = (group && group.slots && group.slots.length) ? group.slots : ['7AM-8AM', '8AM-9AM'];
      ['fexp_slot', 'fmem_slot', 'fwalk_slot'].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const prev = sel.value;
        let opts = slots.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
        // Expenses and walk-ins can be group-wide (no slot): the cost/fee is
        // then split across ALL distinct Yes members that month. Membership
        // always needs a real slot.
        if (id === 'fexp_slot' || id === 'fwalk_slot') {
          opts += `<option value="">\u2014 whole group (no slot) \u2014</option>`;
        }
        sel.innerHTML = opts;
        // Restore the last slot you used (QoL), else keep the current value.
        const remembered = (id === 'fmem_slot') ? _rememberedFinance('slot') : null;
        const want = (remembered && [...sel.options].some(o => o.value === remembered)) ? remembered : prev;
        if ([...sel.options].some(o => o.value === want)) sel.value = want;
      });
    }

    // Small QoL: remember the finance month/slot you last worked with so you
    // don't re-pick every visit. Guarded so it never throws or triggers loads.
    function _rememberedFinance(key) {
      try { return (JSON.parse(localStorage.getItem('nw_finance_sel') || '{}'))[key] || null; }
      catch (_) { return null; }
    }
    function _rememberFinance(key, val) {
      try {
        const o = JSON.parse(localStorage.getItem('nw_finance_sel') || '{}');
        o[key] = val; localStorage.setItem('nw_finance_sel', JSON.stringify(o));
      } catch (_) {}
    }
    function restoreFinanceMonth() {
      const m = _rememberedFinance('month');
      if (!m) return;
      ['fexp_month', 'fmem_month', 'fwalk_month'].forEach(id => {
        const sel = document.getElementById(id);
        if (sel && [...(sel.options || [])].some(o => o.value === m)) sel.value = m;
      });
    }

    function populateFinanceGroups() {
      const sel = document.getElementById('finance_group_select');
      if (!sel) return;
      const mine = myPlayerId();
      const visible = (allGroups || []).filter(g => {
        if (isSuperAdmin()) return true;
        if (canManageGroup(g)) return true;
        return mine && (g.finance_roles || {})[mine];
      });
      sel.innerHTML = '';
      visible.forEach(g => {
        const o = document.createElement('option');
        o.value = g.group_id;
        o.textContent = g.group_name || g.name || g.group_id;
        sel.appendChild(o);
      });
      // Prefer the default club ledger as the initial selection.
      const def = visible.find(g => (g.group_name || g.name) === 'Club (default)');
      currentFinanceGroupId = (def && def.group_id) || (visible[0] && visible[0].group_id) || null;
      if (currentFinanceGroupId) sel.value = currentFinanceGroupId;
      populateFinanceSlots(visible.find(g => g.group_id === currentFinanceGroupId));
      const hint = document.getElementById('finance-group-hint');
      if (hint) hint.textContent = visible.length > 1
        ? 'Switch which group\u2019s finances you\u2019re viewing.'
        : '';
      document.getElementById('finance-group-card').style.display = visible.length > 1 ? '' : 'none';
    }

    function reloadFinanceForGroup() {
      loadFinanceSummary(); loadFinanceExpenses(); loadFinanceMembers();
      if (typeof loadFinanceWalkins === 'function') loadFinanceWalkins();
    }

    async function tryAutoFinanceUnlock() {
      if (!isLoggedIn()) return false;
      try {
        const { res, data } = await authedFetch(`${API_BASE_URL}/finance-access`);
        if (!res.ok || !data.view_key) return false;
        financeKey = data.view_key;
        myFinanceRole = data.finance_role || 'view';
        sessionStorage.setItem('nw_finance_key', financeKey);
        document.getElementById('finance-content').style.display = 'block';
        document.getElementById('finance-lock-card').style.display = 'none';
        applyFinanceRoleVisibility();
        populateFinanceGroups();
        const s = await (await fetch(`${financeBaseUrl()}/settings?${finQS()}`, { headers: getAuthHeaders() })).json();
        document.getElementById('finance_walkins_public').checked = !!s.walkins_public;
        document.getElementById('finance_upi_id').value = s.upi_id || '';
        document.getElementById('finance_upi_name').value = s.upi_name || '';
        loadFinanceSummary(); loadFinanceExpenses(); loadFinanceMembers();
        const lock = document.getElementById('finance-lock-status');
        if (lock) lock.textContent = 'Unlocked ✓ (you have finance access)';
        return true;
      } catch (e) { return false; }
    }

    // Member self-settlement: what I owe / am owed, my slots only. Available
    // to any logged-in member without the view key or a finance role.
    function myFinanceGroups() {
      const mine = myPlayerId();
      return (allGroups || []).filter(g => mine && (g.member_ids || []).includes(mine));
    }

    function populateMyDuesGroups() {
      const card = document.getElementById('my-dues-card');
      const sel = document.getElementById('my_dues_group_select');
      const label = document.getElementById('my-dues-group-label');
      if (!card || !isLoggedIn() || !hasLinkedPlayer()) { if (card) card.style.display = 'none'; return; }
      const groups = myFinanceGroups();
      if (!groups.length) { card.style.display = 'none'; return; }
      card.style.display = '';
      sel.innerHTML = '';
      groups.forEach(g => {
        const o = document.createElement('option');
        o.value = g.group_id; o.textContent = g.group_name || g.group_id;
        sel.appendChild(o);
      });
      label.style.display = groups.length > 1 ? '' : 'none';
      loadMyDues(sel.value);
    }

    async function loadMyDues(groupId) {
      const el = document.getElementById('my-dues-result');
      if (!el) return;
      el.textContent = 'Loading...';
      try {
        const base = getAuthHeaders().Authorization ? `${API_BASE_URL}/finance-secure` : `${API_BASE_URL}/finance`;
        const { res, data } = await authedFetch(`${base}/my-settlement?group_id=${encodeURIComponent(groupId)}`);
        if (!res.ok) { el.textContent = `Error: ${data.error || 'could not load'}`; return; }
        const lines = data.lines || [];
        if (!lines.length) { el.innerHTML = '<p style="color:var(--text-secondary);">No dues on record for you in this group yet.</p>'; return; }
        let html = '<table><tr><th>Month</th><th>Slot</th><th>You owe</th><th>Owed back</th></tr>';
        lines.forEach(l => {
          html += `<tr><td>${l.month} ${l.year}</td><td>${l.slot}</td>`
                + `<td>${l.you_paid ? '<span style="color:var(--court,#2fa968);">paid</span>' : '\u20b9' + l.you_owe}</td>`
                + `<td>${l.owed_back_to_you ? '\u20b9' + l.owed_back_to_you : '-'}</td></tr>`;
        });
        html += '</table>';
        const net = data.net;
        const netMsg = net > 0
          ? `<strong style="color:var(--court,#2fa968);">The club owes you \u20b9${net}</strong>`
          : net < 0
            ? `<strong>You owe \u20b9${Math.abs(net)}</strong>`
            : '<strong>You\u2019re all settled up.</strong>';
        html += `<p style="margin-top:10px;">${netMsg}</p>`;

        // UPI tap-to-pay (Stage 6). Builds a upi://pay deep-link the phone
        // hands to the user's UPI app. NetWorth never processes the payment
        // and gets no confirmation, so paying here does NOT mark you paid -
        // that stays a manual step an owner does. The amount is editable in
        // the UPI app, as UPI always allows.
        const owe = data.total_you_owe || 0;
        const payee = data.payee || {};
        if (owe > 0 && payee.upi_id) {
          const note = encodeURIComponent(`${data.group_name || 'NetWorth'} dues`);
          const link = `upi://pay?pa=${encodeURIComponent(payee.upi_id)}&pn=${encodeURIComponent(payee.upi_name || '')}&am=${owe}&cu=INR&tn=${note}`;
          html += `<a href="${link}" class="btn" style="display:inline-block; margin-top:6px; padding:8px 16px; background:var(--court,#2fa968); color:#fff; border-radius:8px; text-decoration:none; font-weight:600;">Pay \u20b9${owe} via UPI</a>`;
          html += `<p style="font-size:11px; color:var(--text-secondary); margin:6px 0 0;">Opens your UPI app. Payment isn\u2019t auto-confirmed here \u2014 an owner marks it paid once received.</p>`;
        } else if (owe > 0 && !payee.upi_id) {
          html += '<p style="font-size:12px; color:var(--text-secondary); margin-top:6px;">Ask an owner to set a payment (UPI) account for this group to pay here.</p>';
        }
        el.innerHTML = html;
      } catch (e) { el.textContent = `Could not load: ${e.message}`; }
    }

    // Owner/admin: edit the group's slot list (comma-separated).
    async function manageGroupSlots(groupId) {
      const cur = await (await fetch(`${API_BASE_URL}/groups/${groupId}`)).json();
      const existing = (cur.slots || []).join(', ');
      const input = await nwPrompt('Time slots for this group, comma-separated\n(e.g. 7-8AM, 8-9AM):', existing);
      if (input === null) return;
      const slots = input.split(',').map(s => s.trim()).filter(Boolean);
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/group-slots/${groupId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slots })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadGroupMembers(groupId);
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    // Owner/admin: assign members to a slot. Prompts with nicknames; resolves
    // to player_ids against the group's current members.
    async function assignSlotMembers(groupId, slotEnc) {
      const slot = decodeURIComponent(slotEnc);
      const cur = await (await fetch(`${API_BASE_URL}/groups/${groupId}`)).json();
      const members = cur.members || [];
      const byNick = {};
      members.forEach(m => { byNick[(m.nickname || '').toLowerCase()] = m.player_id; byNick[(m.name || '').toLowerCase()] = m.player_id; });
      const currentPids = (cur.slot_members || {})[slot] || [];
      const currentNicks = currentPids.map(pid => {
        const m = members.find(x => x.player_id === pid); return m ? (m.nickname || m.name) : pid;
      }).join(', ');
      const input = await nwPrompt(`Who plays the ${slot} slot? Comma-separated nicknames:`, currentNicks);
      if (input === null) return;
      const wanted = input.split(',').map(s => s.trim()).filter(Boolean);
      const pids = [];
      const unknown = [];
      wanted.forEach(w => { const pid = byNick[w.toLowerCase()]; if (pid) pids.push(pid); else unknown.push(w); });
      if (unknown.length && !await nwConfirm(`Not found in this group (will be skipped): ${unknown.join(', ')}\n\nContinue?`)) return;
      const slotMembers = { ...(cur.slot_members || {}), [slot]: pids };
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/group-slots/${groupId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slot_members: slotMembers })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadGroupMembers(groupId);
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    async function transferGroupOwnership(groupId) {
      const cur = await (await fetch(`${API_BASE_URL}/groups/${groupId}`)).json();
      const members = cur.members || [];
      const nick = await nwPrompt('Transfer ownership to which member? Enter their nickname.\n\nYou will become a regular member (view access) afterwards.');
      if (nick === null) return;
      const target = members.find(m => (m.nickname || '').toLowerCase() === nick.trim().toLowerCase()
        || (m.name || '').toLowerCase() === nick.trim().toLowerCase());
      if (!target) { nwAlert('No member with that nickname in this group.'); return; }
      if (!await nwConfirm(`Make ${target.nickname || target.name} the owner? You will be demoted to a regular member.`)) return;
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/group-slots/${groupId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transfer_to: target.player_id })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadGroupMembers(groupId);
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    async function setGroupPayee(groupId) {
      const cur = await (await fetch(`${API_BASE_URL}/groups/${groupId}`)).json();
      const members = cur.members || [];
      const existing = cur.finance_payee || {};
      const nick = await nwPrompt('Who collects payments for this group? Enter their nickname (must be a member):',
        existing.player_id ? (members.find(m => m.player_id === existing.player_id) || {}).nickname || '' : '');
      if (nick === null) return;
      const target = members.find(m => (m.nickname || '').toLowerCase() === nick.trim().toLowerCase()
        || (m.name || '').toLowerCase() === nick.trim().toLowerCase());
      if (!target) { nwAlert('No member with that nickname in this group.'); return; }
      const upi = await nwPrompt(`UPI ID for ${target.nickname || target.name} (e.g. name@bank):`, existing.upi_id || '');
      if (upi === null) return;
      const upiName = await nwPrompt('Name shown on the payment (optional):', existing.upi_name || target.name || '');
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/group-slots/${groupId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ finance_payee: { player_id: target.player_id, upi_id: (upi || '').trim(), upi_name: (upiName || '').trim() } })
        });
        if (!res.ok) { nwAlert(`Error: ${error}`); return; }
        loadGroupMembers(groupId);
      } catch (e) { nwAlert(`Failed: ${e.message}`); }
    }

    async function requestFinanceAccess() {
      const el = document.getElementById('finance-request-status');
      if (!isLoggedIn() || !hasLinkedPlayer()) { el.textContent = 'Log in and link your profile first.'; return; }
      const role = await nwPrompt('What finance access do you need?\n\nType one of: view, write, delete\n\n- view: see the numbers\n- write: add and edit records\n- delete: also remove records', 'view');
      if (role === null) return;
      const r = role.trim().toLowerCase();
      if (!['view','write','delete'].includes(r)) { el.textContent = 'Type view, write or delete.'; return; }
      el.textContent = 'Sending...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/action-request`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'finance_access', role: r, group_id: currentFinanceGroupId })
        });
        el.textContent = res.ok ? `Request for ${r} access sent - the group owner will review it.` : `Error: ${error}`;
      } catch (e) { el.textContent = `Failed: ${e.message}`; }
    }

    async function financeUnlock() {
      const statusEl = document.getElementById('finance-lock-status');
      financeKey = document.getElementById('finance_view_key').value.trim();
      statusEl.textContent = 'Checking...';
      // Wrapped: an unhandled network error here used to leave "Checking..."
      // on screen forever, since nothing reset it on a thrown fetch.
      try {
        const res = await fetch(`${financeBaseUrl()}/settings?${finQS()}`, { headers: getAuthHeaders() });
        if (!res.ok) {
          statusEl.textContent = res.status === 403 ? 'Incorrect key' : `Error ${res.status}`;
          document.getElementById('finance-content').style.display = 'none';
          return;
        }
        sessionStorage.setItem('nw_finance_key', financeKey);
        statusEl.textContent = 'Unlocked ✓';
        document.getElementById('finance-content').style.display = 'block';
        document.getElementById('finance-lock-card').style.display = 'none';
        // Someone unlocking with the raw key has full rights (the key was
        // always all-or-nothing); a logged-in user's role, if any, is
        // fetched to hide controls they shouldn't see.
        try {
          const rr = await authedFetch(`${API_BASE_URL}/finance-access`);
          myFinanceRole = (rr.res.ok && rr.data.finance_role) ? rr.data.finance_role : 'delete';
        } catch (_) { myFinanceRole = 'delete'; }
        applyFinanceRoleVisibility();
        const settings = await res.json();
        document.getElementById('finance_walkins_public').checked = !!settings.walkins_public;
        document.getElementById('finance_upi_id').value = settings.upi_id || '';
        document.getElementById('finance_upi_name').value = settings.upi_name || '';
        loadFinanceSummary();
        loadFinanceExpenses();
        loadFinanceMembers();
        loadFinanceWalkins();
      } catch (e) {
        statusEl.textContent = `Couldn't reach finance - ${e.message}`;
      }
    }

    async function loadFinanceSummary() {
      const el = document.getElementById('finance-summary-result');
      el.textContent = 'Loading...';
      const res = await fetch(`${financeBaseUrl()}/summary?${finQS()}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      if (!data.summary.length) { el.textContent = 'No finance data yet.'; return; }
      let html = '<table><tr><th>Month</th><th>Slot</th><th>Estimated</th><th>Actual</th><th>Extra collected</th><th>Members</th><th>Per head</th><th>Residual / head</th><th>Status</th></tr>';
      data.summary.forEach(r => {
        const perHead = r.cost_per_head === null ? 'pending'
          : `${r.cost_per_head}<br><span style="font-size:11px;opacity:0.65;">${r.estimated_total}÷${r.player_count}</span>`;
        const status = r.collection_status === 'settled' ? 'Settled ✓'
          : r.collection_status === 'collecting' ? `Collecting ${r.confirmed_count}/${r.player_count}` : '-';
        html += `<tr><td>${r.month} ${r.year}</td><td>${r.slot}</td><td>${r.estimated_total}</td><td>${r.actual_total}</td>` +
          `<td>${r.extra_collected}</td><td>${r.player_count}</td>` +
          `<td>${perHead}</td>` +
          `<td>${r.residual_per_head === null ? 'pending' : r.residual_per_head}</td><td>${status}</td></tr>`;
      });
      el.innerHTML = html + '</table>';
    }

    async function loadFinanceExpenses() {
      const el = document.getElementById('finance-expenses-result');
      el.textContent = 'Loading...';
      const res = await fetch(`${financeBaseUrl()}/expenses?${finQS()}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      if (!data.expenses.length) { el.textContent = 'No expenses yet.'; return; }
      lastExpenses = data.expenses;
      let html = '<table><tr><th>Month</th><th>Slot</th><th>Item</th><th>Est cost</th><th>Act cost</th><th>Est qty</th><th>Act qty</th><th></th></tr>';
      data.expenses.forEach(e => {
        html += `<tr><td>${e.month} ${e.year}</td><td>${e.slot}</td><td>${e.item}</td>` +
          `<td>${e.estimated_cost ?? ''}</td><td>${e.actual_cost ?? ''}</td><td>${e.estimated_qty ?? ''}</td><td>${e.actual_qty ?? ''}</td>` +
          `<td><button type="button" class="secondary fin-edit-exp" data-id="${e.record_id}">Edit</button> ` +
          `<button type="button" class="secondary fin-del" data-kind="expenses" data-id="${e.record_id}">Delete</button></td></tr>`;
      });
      el.innerHTML = html + '</table>';
      el.querySelectorAll('.fin-edit-exp').forEach(btn => btn.addEventListener('click', () => {
        const e = lastExpenses.find(x => x.record_id === btn.dataset.id);
        if (!e) return;
        editingExpenseId = e.record_id;
        document.getElementById('fexp_month').value = e.month;
        document.getElementById('fexp_year').value = e.year;
        document.getElementById('fexp_slot').value = e.slot;
        document.getElementById('fexp_item').value = e.item || '';
        document.getElementById('fexp_est_cost').value = e.estimated_cost ?? '';
        document.getElementById('fexp_act_cost').value = e.actual_cost ?? '';
        document.getElementById('fexp_est_qty').value = e.estimated_qty ?? '';
        document.getElementById('fexp_act_qty').value = e.actual_qty ?? '';
        document.getElementById('finance-add-expense-btn').textContent = 'Save changes';
        document.getElementById('finance-cancel-expense-edit-btn').style.display = 'inline-block';
        document.getElementById('fexp_item').scrollIntoView({ behavior: 'smooth', block: 'center' });
      }));
      applyFinanceRoleVisibility();
    }

    let editingExpenseId = null;
    let lastExpenses = [];

    function resetExpenseEdit() {
      editingExpenseId = null;
      document.getElementById('finance-add-expense-btn').textContent = 'Add expense';
      document.getElementById('finance-cancel-expense-edit-btn').style.display = 'none';
      document.getElementById('fexp_item').value = '';
    }

    async function addFinanceExpense() {
      const body = {
        month: document.getElementById('fexp_month').value,
        year: document.getElementById('fexp_year').value,
        slot: document.getElementById('fexp_slot').value,
        item: document.getElementById('fexp_item').value.trim(),
        estimated_cost: document.getElementById('fexp_est_cost').value,
        actual_cost: document.getElementById('fexp_act_cost').value,
        estimated_qty: document.getElementById('fexp_est_qty').value,
        actual_qty: document.getElementById('fexp_act_qty').value,
      };
      const { ok, data } = editingExpenseId
        ? await finPost(`expenses/${editingExpenseId}`, 'PUT', body)
        : await finPost('expenses', 'POST', body);
      if (!ok || (data.errors && data.errors.length)) { nwAlert('Error: ' + JSON.stringify(data.errors || data.error)); return; }
      resetExpenseEdit();
      loadFinanceExpenses(); loadFinanceSummary(); loadFinanceMembers();
    }

    async function loadFinanceMembers() {
      const el = document.getElementById('finance-members-result');
      el.textContent = 'Loading...';
      const qs = finQS({
        month: document.getElementById('fmem_month').value,
        year: document.getElementById('fmem_year').value,
        slot: document.getElementById('fmem_slot').value,
      });
      const res = await fetch(`${financeBaseUrl()}/memberships?${qs}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      if (!data.memberships.length) { el.textContent = 'No membership rows for this month/slot yet.'; renderBulkRosterList(); return; }
      lastMemberships = data.memberships;
      const yesCount = data.memberships.filter(m => m.status === 'Yes').length;
      const cph = data.cost_per_head;
      let html = `<p style="font-size:13px;">Enrolled (Yes): <strong>${yesCount}</strong>` +
        (cph != null ? ` · Per head: <strong>${cph}</strong> <span style="opacity:0.65;">(${data.estimated_total}÷${yesCount})</span>` : '') + `</p>`;
      data.memberships.forEach(m => {
        let pay = '';
        if (m.status === 'Yes' && cph != null) {
          // What they actually pay = per-head minus their relief (from the
          // backend). Shown right on the card so there's no trip to Insights.
          const eff = (m.effective != null) ? m.effective : cph;
          const reliefNote = (m.relief && m.relief > 0)
            ? ` <span style="font-size:11px;opacity:0.7;">(₹${cph} − ₹${m.relief} relief)</span>` : '';
          const conf = m.payment_confirmed_amount != null ? parseFloat(m.payment_confirmed_amount) : null;
          if (conf !== null && Math.abs(conf - eff) < 0.01) {
            pay = `<div class="fin-mem-card-pay">Pay <strong>₹${eff}</strong>${reliefNote} · Paid ✓ <button type="button" class="secondary fin-unconfirm" data-id="${m.record_id}">undo</button></div>`;
          } else if (conf !== null) {
            pay = `<div class="fin-mem-card-pay">Pay <strong>₹${eff}</strong>${reliefNote} <button type="button" class="fin-confirm" data-id="${m.record_id}" data-name="${m.display_name}" data-eff="${eff}">Reconfirm</button> <span style="font-size:11px;opacity:0.65;">amount changed (was ₹${conf})</span></div>`;
          } else {
            pay = `<div class="fin-mem-card-pay">Pay <strong>₹${eff}</strong>${reliefNote} <button type="button" class="fin-confirm" data-id="${m.record_id}" data-name="${m.display_name}" data-eff="${eff}">Confirm payment</button></div>`;
          }
        }
        html += `<div class="fin-mem-card">
          <div class="fin-mem-card-top">
            <span class="fin-mem-card-name">${m.display_name}${m.player_id ? '' : ' <span style="opacity:0.6;">(unlinked)</span>'}</span>
            <select class="fin-mem-status" data-id="${m.record_id}">${['Yes', 'No', 'NA'].map(s => `<option${s === m.status ? ' selected' : ''}>${s}</option>`).join('')}</select>
          </div>
          ${m.remark ? `<div style="font-size:12px;opacity:0.7;margin-top:4px;">${m.remark}</div>` : ''}
          ${pay}
          <div class="fin-mem-card-actions">
            <button type="button" class="secondary fin-refund" data-name="${m.display_name}" data-pid="${m.player_id || ''}">Refund…</button>
            <button type="button" class="secondary fin-forfeit" data-id="${m.record_id}" data-on="${m.forfeit_residual ? '1' : ''}" title="Forfeit this month's refund and redistribute it to the other members">${m.forfeit_residual ? '\u2713 Refund forfeited' : 'Forfeit refund'}</button>
            <button type="button" class="secondary fin-del" data-kind="memberships" data-id="${m.record_id}">Delete</button>
          </div>
        </div>`;
      });
      el.innerHTML = html;
      el.querySelectorAll('.fin-forfeit').forEach(btn => btn.addEventListener('click', async () => {
        const turningOn = !btn.dataset.on;
        const msg = turningOn
          ? 'Forfeit this member\u2019s refund for this month? Their share is redistributed to the other members (they get \u20b90 back). This does not change what they paid.'
          : 'Restore this member\u2019s refund (undo forfeit)?';
        if (!await nwConfirm(msg)) return;
        const { ok, data: d } = await finPost(`memberships/${btn.dataset.id}`, 'PUT', { forfeit_residual: turningOn });
        if (!ok) { nwAlert('Error: ' + d.error); return; }
        loadFinanceMembers(); loadFinanceSummary();
      }));
      el.querySelectorAll('.fin-confirm').forEach(btn => btn.addEventListener('click', async () => {
        if (!await nwConfirm(`Confirm that ${btn.dataset.name} paid ₹${btn.dataset.eff} for ${document.getElementById('fmem_month').value} ${document.getElementById('fmem_slot').value}?`)) return;
        const { ok, data: d } = await finPost(`memberships/${btn.dataset.id}`, 'PUT', { confirm_payment: true });
        if (!ok) nwAlert('Error: ' + d.error);
        loadFinanceMembers(); loadFinanceSummary();
      }));
      el.querySelectorAll('.fin-unconfirm').forEach(btn => btn.addEventListener('click', async () => {
        if (!await nwConfirm('Mark this payment as NOT confirmed?')) return;
        const { ok, data: d } = await finPost(`memberships/${btn.dataset.id}`, 'PUT', { confirm_payment: false });
        if (!ok) nwAlert('Error: ' + d.error);
        loadFinanceMembers(); loadFinanceSummary();
      }));
      el.querySelectorAll('.fin-refund').forEach(btn => btn.addEventListener('click', async () => {
        const amount = parseFloat(await nwPrompt(`Refund amount for ${btn.dataset.name} (paid back from this slot's walk-in collection):`));
        if (!amount || amount <= 0) return;
        const reason = await nwPrompt('Reason (e.g. medical - ankle fracture):') || 'compassionate refund';
        if (!await nwConfirm(`Issue a refund of ${amount} to ${btn.dataset.name}? Reason: ${reason}`)) return;
        const body = {
          date: new Date().toISOString().slice(0, 10),
          slot: document.getElementById('fmem_slot').value,
          display_name: btn.dataset.name,
          fee: -amount,
          note: `refund: ${reason}`,
        };
        if (btn.dataset.pid) body.player_id = btn.dataset.pid;
        const { ok, data: d } = await finPost('walkins', 'POST', body);
        if (!ok) { nwAlert('Error: ' + JSON.stringify(d.errors || d.error)); return; }
        nwAlert('Refund recorded as a negative walk-in entry.');
        loadFinanceWalkins(); loadFinanceSummary(); loadFinanceMembers();
      }));
      el.querySelectorAll('.fin-mem-status').forEach(sel => sel.addEventListener('change', async (e) => {
        e.target.disabled = true;
        const { ok, data: d } = await finPost(`memberships/${e.target.dataset.id}`, 'PUT', { status: e.target.value });
        e.target.disabled = false;
        if (!ok) { nwAlert('Error: ' + d.error); return; }
        // Saved immediately (data is safe), but per-head amounts only need
        // recomputing once you're done toggling - so instead of reloading the
        // whole section on every change (jarring when you set 10 members),
        // surface a Recalculate button and let the user trigger it once.
        markMembersDirty();
      }));
      renderBulkRosterList();
      applyFinanceRoleVisibility();
    }

    function markMembersDirty() {
      const btn = document.getElementById('finance-recalc-btn');
      const hint = document.getElementById('finance-recalc-hint');
      if (btn) btn.style.display = 'inline-block';
      if (hint) hint.style.display = 'inline';
    }

    function recalcMembers() {
      const btn = document.getElementById('finance-recalc-btn');
      const hint = document.getElementById('finance-recalc-hint');
      if (btn) btn.style.display = 'none';
      if (hint) hint.style.display = 'none';
      loadFinanceSummary();
      loadFinanceMembers();
    }

    let lastMemberships = [];
    const monthNamesFull = ['January','February','March','April','May','June','July','August','September','October','November','December'];

    function renderBulkRosterList() {
      const el = document.getElementById('bulk-roster-list');
      if (!el) return;
      const already = new Set(lastMemberships.map(m => m.player_id).filter(Boolean));
      if (!allPlayers.length) { el.innerHTML = '<p style="font-size:13px;">No players registered yet.</p>'; return; }
      el.innerHTML = allPlayers.map(p => {
        const disabled = already.has(p.player_id);
        return `<label class="bulk-roster-item" style="${disabled ? 'opacity:0.45;' : ''}">
          <input type="checkbox" value="${p.player_id}" data-name="${p.name}" ${disabled ? 'disabled' : ''}>
          ${p.name}${disabled ? ' <span style="font-size:11px;">(already on this month/slot)</span>' : ''}
        </label>`;
      }).join('');
    }

    async function bulkAddFromRoster() {
      const checked = [...document.querySelectorAll('#bulk-roster-list input:checked')];
      if (!checked.length) { nwAlert('Tick at least one player.'); return; }
      const status = document.getElementById('bulk_status').value;
      const items = checked.map(cb => ({
        month: document.getElementById('fmem_month').value,
        year: document.getElementById('fmem_year').value,
        slot: document.getElementById('fmem_slot').value,
        display_name: cb.dataset.name, player_id: cb.value, status,
      }));
      const { ok, data } = await finPost('memberships', 'POST', { items });
      if (!ok || (data.errors && data.errors.length)) { nwAlert('Error: ' + JSON.stringify(data.errors || data.error)); return; }
      loadFinanceMembers(); loadFinanceSummary();
    }

    async function copyPreviousMonthMembers() {
      const month = document.getElementById('fmem_month').value;
      const year = parseInt(document.getElementById('fmem_year').value, 10);
      const slot = document.getElementById('fmem_slot').value;
      const idx = monthNamesFull.indexOf(month);
      const prevMonth = monthNamesFull[(idx + 11) % 12];
      const prevYear = idx === 0 ? year - 1 : year;
      const qs = finQS({ month: prevMonth, year: prevYear, slot });
      const res = await fetch(`${financeBaseUrl()}/memberships?${qs}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { nwAlert('Error: ' + data.error); return; }
      const prevYes = (data.memberships || []).filter(m => m.status === 'Yes');
      if (!prevYes.length) { nwAlert(`No Yes members found for ${prevMonth} ${prevYear} ${slot}.`); return; }
      // Dedup against the CURRENT month/slot as it actually is on the server
      // right now - not against lastMemberships, which may still hold a
      // different month/slot the user was viewing (that stale compare was
      // making an empty target wrongly report "everyone's already here").
      const curQs = finQS({ month, year, slot });
      let currentMembers = [];
      try {
        const curRes = await fetch(`${financeBaseUrl()}/memberships?${curQs}`, { headers: getAuthHeaders() });
        const curData = await curRes.json();
        if (curRes.ok) currentMembers = curData.memberships || [];
      } catch (_) { /* treat as empty target */ }
      const existingNames = new Set(currentMembers.map(m => m.player_id || `name:${m.display_name}`));
      const toAdd = prevYes.filter(m => !existingNames.has(m.player_id || `name:${m.display_name}`));
      if (!toAdd.length) { nwAlert('Everyone from last month is already on this month/slot.'); return; }
      if (!await nwConfirm(`Add ${toAdd.length} member(s) from ${prevMonth} ${prevYear} to ${month} ${year} ${slot}, status No (pending renewal)?`)) return;
      const items = toAdd.map(m => ({
        month, year, slot, display_name: m.display_name, player_id: m.player_id, status: 'No'
      }));
      const { ok, data: d } = await finPost('memberships', 'POST', { items });
      if (!ok || (d.errors && d.errors.length)) { nwAlert('Error: ' + JSON.stringify(d.errors || d.error)); return; }
      loadFinanceMembers(); loadFinanceSummary();
    }

    async function addFinanceMember() {
      const pid = document.getElementById('fmem_player').value;
      const name = document.getElementById('fmem_name').value.trim();
      const chosen = pid ? allPlayers.find(p => p.player_id === pid) : null;
      if (!chosen && !name) { nwAlert('Pick a roster player or type a name'); return; }
      const body = {
        month: document.getElementById('fmem_month').value,
        year: document.getElementById('fmem_year').value,
        slot: document.getElementById('fmem_slot').value,
        display_name: chosen ? chosen.name : name,
        status: document.getElementById('fmem_status').value,
        remark: document.getElementById('fmem_remark').value.trim(),
      };
      if (chosen) body.player_id = pid;
      const { ok, data } = await finPost('memberships', 'POST', body);
      if (!ok || (data.errors && data.errors.length)) { nwAlert('Error: ' + JSON.stringify(data.errors || data.error)); return; }
      document.getElementById('fmem_name').value = '';
      document.getElementById('fmem_remark').value = '';
      loadFinanceMembers(); loadFinanceSummary();
    }

    async function loadFinanceWalkins() {
      const el = document.getElementById('finance-walkins-result');
      el.textContent = 'Loading...';
      const fMonth = document.getElementById('fwalk_filter_month').value;
      const fYear = document.getElementById('fwalk_filter_year').value;
      const filters = {};
      if (fYear) { filters.year = fYear; if (fMonth) filters.month = fMonth; }
      const res = await fetch(`${financeBaseUrl()}/walkins?${finQS(filters)}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      if (!data.walkins.length) { el.textContent = 'No walk-ins for this period.'; return; }
      let html = '<table><tr><th>Date</th><th>Slot</th><th>Name</th><th>Fee</th><th>Skill</th><th>Recruit?</th><th></th></tr>';
      data.walkins.forEach(w => {
        // Linked to a roster player -> show their CURRENT name/nickname,
        // toggle-aware, ignoring the frozen display_name snapshot from
        // whenever the walk-in was recorded. A true guest (no player_id
        // at all) has no nickname concept, so their typed name stays as-is.
        const label = w.player_id ? playerLabelById(w.player_id, w.display_name) : w.display_name;
        html += `<tr><td>${w.date}</td><td>${w.slot}</td>` +
          `<td>${label}${w.player_id ? '' : ' <span style="opacity:0.6;">(guest)</span>'}${w.note ? ` <span title="${String(w.note).replace(/"/g, '&quot;')}" onclick="nwAlert('${String(w.note).replace(/'/g, "\\'").replace(/"/g, '&quot;')}')">📝</span>` : ''}</td>` +
          `<td>${w.fee}</td><td>${w.skill || ''}</td><td>${w.recruit_verdict || ''}</td>` +
          `<td><button type="button" class="secondary fin-del" data-kind="walkins" data-id="${w.record_id}">Delete</button></td></tr>`;
      });
      el.innerHTML = html + '</table>';
      applyFinanceRoleVisibility();
    }

    async function addFinanceWalkin() {
      const pid = document.getElementById('fwalk_player').value;
      const name = document.getElementById('fwalk_name').value.trim();
      const chosen = pid ? allPlayers.find(p => p.player_id === pid) : null;
      if (!chosen && !name) { nwAlert('Pick a roster player or type a guest name'); return; }
      if (!document.getElementById('fwalk_date').value) { nwAlert('Pick a date'); return; }
      const body = {
        date: document.getElementById('fwalk_date').value,
        slot: document.getElementById('fwalk_slot').value,
        display_name: chosen ? chosen.name : name,
        fee: document.getElementById('fwalk_fee').value,
        skill: document.getElementById('fwalk_skill').value.trim(),
        recruit_verdict: document.getElementById('fwalk_recruit').value,
        note: document.getElementById('fwalk_note').value.trim(),
      };
      if (chosen) body.player_id = pid;
      const { ok, data } = await finPost('walkins', 'POST', body);
      if (!ok || (data.errors && data.errors.length)) { nwAlert('Error: ' + JSON.stringify(data.errors || data.error)); return; }
      document.getElementById('fwalk_name').value = '';
      document.getElementById('fwalk_note').value = '';
      // Prepend the new row locally instead of re-fetching the whole table;
      // the settlement refresh happens quietly in the background.
      const tbl = document.querySelector('#finance-walkins-result table');
      if (tbl && data.created && data.created.length) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${body.date}</td><td>${body.slot}</td>` +
          `<td>${body.display_name}${body.player_id ? '' : ' <span style="opacity:0.6;">(guest)</span>'}</td>` +
          `<td>${body.fee}</td><td>${body.skill || ''}</td><td>${body.recruit_verdict || ''}</td>` +
          `<td><button type="button" class="secondary fin-del" data-kind="walkins" data-id="${data.created[0]}">Delete</button></td>`;
        tbl.insertBefore(tr, tbl.rows[1] || null);
      } else {
        loadFinanceWalkins();
      }
      loadFinanceSummary();
    }

    let lastInsights = null;

    async function loadFinanceInsights() {
      const el = document.getElementById('finance-insights-result');
      el.textContent = 'Loading...';
      const filters = {};
      const fm = document.getElementById('fins_month').value;
      const fy = document.getElementById('fins_year').value;
      if (fy) { filters.year = fy; if (fm) filters.month = fm; }
      const res = await fetch(`${financeBaseUrl()}/insights?${finQS(filters)}`, { headers: getAuthHeaders() });
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      lastInsights = data;
      renderInsights();
    }

    function copyDuesForWhatsApp() {
      const data = lastInsights;
      if (!data || !data.cost_rows || !data.cost_rows.length) { nwAlert('Load insights first.'); return; }
      const month = data.cost_rows[0].month, year = data.cost_rows[0].year;
      const rows = data.cost_rows.map(r => ({
        name: r.display_name,
        owed: r.paid === null ? null : Number(r.paid),
        relief: (r.relief && r.relief != 0) ? Number(r.relief) : 0,
        pay: r.effective_cost === null ? null : Number(r.effective_cost),
      }));
      const money = v => (v === null ? '-' : Number(v).toFixed(2));
      // Column widths sized to the longest cell so it lines up in WhatsApp's
      // monospace (```-wrapped) rendering.
      const nameW = Math.max(6, ...rows.map(r => r.name.length));
      const owedW = Math.max(4, ...rows.map(r => money(r.owed).length));
      const relW = Math.max(6, ...rows.map(r => money(r.relief).length));
      const payW = Math.max(3, ...rows.map(r => money(r.pay).length));
      const pad = (s, w) => String(s).padEnd(w);
      const padL = (s, w) => String(s).padStart(w);
      const line = (n, o, r, p) => `${pad(n, nameW)}  ${padL(o, owedW)}  ${padL(r, relW)}  ${padL(p, payW)}`;
      const out = ['```', line('Member', 'Owed', 'Relief', 'Pay')];
      let tOwed = 0, tRel = 0, tPay = 0;
      rows.forEach(r => {
        out.push(line(r.name, money(r.owed), r.relief ? money(r.relief) : '-', money(r.pay)));
        tOwed += r.owed || 0; tRel += r.relief || 0; tPay += r.pay || 0;
      });
      out.push(line('TOTAL', money(tOwed), money(tRel), money(tPay)));
      out.push('```');
      const text = `*${month} ${year} dues*\n` + out.join('\n');
      const done = () => nwAlert('Copied - paste into your WhatsApp group (keep the ``` for the table to line up).');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
      } else { fallbackCopy(text, done); }
    }

    async function fallbackCopy(text, cb) {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); cb && cb(); }
      catch (e) { await nwPrompt('Copy this:', text); }
      document.body.removeChild(ta);
    }

    function renderInsights() {
      const el = document.getElementById('finance-insights-result');
      const data = lastInsights;
      if (!data) return;
      const useEst = document.getElementById('fins_estimated').checked;
      let html = '';

      if (data.ghosts && data.ghosts.length) {
        html += '<h4>👻 Ghosts - enrolled but no matches recorded</h4><table><tr><th>Month</th><th>Member</th><th>Slots</th><th></th></tr>';
        data.ghosts.forEach(g => {
          html += `<tr><td>${g.month} ${g.year}</td><td>${g.display_name}</td><td>${g.slots.join(', ')}</td>` +
            `<td><button type="button" class="secondary fin-attended" data-ids="${(g.membership_ids || []).join(',')}" data-name="${g.display_name}">Attended a bit</button></td></tr>`;
        });
        html += '</table>';
      } else {
        html += '<p style="font-size:13px;">👻 No ghosts for this period - everyone enrolled has recorded matches.</p>';
      }
      if (data.noted_attended && data.noted_attended.length) {
        html += '<p style="font-size:12px; opacity:0.75;">Noted as having attended: ' +
          data.noted_attended.map(g => `${g.display_name}${g.attendance_note ? ` (${g.attendance_note})` : ''}`).join(', ') + '</p>';
      }

      if (data.cost_rows && data.cost_rows.length) {
        html += '<h4>💸 Effective monthly cost per member</h4>' +
          '<p class="card-sub">Paid = per-head across all enrolled slots. Relief = last month\'s residual for slots you were in then (discounts this month). Effective = paid − relief.</p>' +
          '<table><tr><th>Month</th><th>Member</th><th>Slots</th><th>Paid</th><th>Relief</th><th>Effective</th><th>Matches</th><th>Cost/match</th></tr>';
        data.cost_rows.forEach(r => {
          const est = useEst && r.estimated_applied;
          const matches = est ? r.matches_estimated : r.matches_actual;
          const cpm = est ? r.cost_per_match_estimated : r.cost_per_match_actual;
          const paidCell = r.paid === null ? 'pending'
            : `${r.paid}<br><span style="font-size:11px;opacity:0.65;">` +
              (r.paid_breakdown || []).filter(b => b.per_head != null)
                .map(b => `${b.slot}: ${b.total}÷${b.members}=${b.per_head}`).join('<br>') + `</span>`;
          html += `<tr><td>${r.month} ${r.year}</td><td>${r.display_name}${r.linked ? '' : ' <span style="opacity:0.6;">(unlinked)</span>'}</td>` +
            `<td>${r.slots.join(', ')}</td>` +
            `<td>${paidCell}</td><td>${r.relief}</td>` +
            `<td>${r.effective_cost === null ? 'pending' : r.effective_cost}</td>` +
            `<td>${matches === null ? '?' : (est ? '~' + matches : matches)}</td>` +
            `<td>${cpm === null ? (r.effective_cost === null ? 'pending' : '∞ (no matches)') : (est ? '~' + cpm : cpm)}</td></tr>`;
        });
        html += '</table>';
        html += '<button type="button" class="secondary" style="margin-top:10px;" onclick="copyDuesForWhatsApp()">Copy for WhatsApp</button>';
        if (data.cost_rows.some(r => r.estimated_applied)) {
          html += `<p style="font-size:12px; opacity:0.7;">~ = estimated: match tracking began ${data.tracking_start || 'mid-month'}; counts for players with under 10 recorded play days include an estimate of ~4.5 games x sessions held before tracking. Untick the box above for captured counts only.</p>`;
        }
      } else {
        html += '<p style="font-size:13px;">💸 No cost rows for this period - either no Yes enrollments yet, or the month predates match tracking.</p>';
      }

      if (data.conversion) {
        const c = data.conversion;
        html += `<h4>🎯 Walk-in conversion</h4><p style="font-size:13px;">${c.became_members} of ${c.total_guests} guests became monthly members.</p>`;
        if (c.guests && c.guests.length) {
          html += '<table><tr><th>Guest</th><th>Sessions</th><th>Fees paid</th><th>Verdict</th><th>Member now?</th></tr>';
          c.guests.forEach(g => {
            html += `<tr><td>${g.display_name}</td><td>${g.sessions}</td><td>${g.fees_paid}</td>` +
              `<td>${g.recruit_verdict || ''}</td><td>${g.became_member ? '✓' : ''}</td></tr>`;
          });
          html += '</table>';
        }
      }
      el.innerHTML = html;
      el.querySelectorAll('.fin-attended').forEach(btn => btn.addEventListener('click', async () => {
        const note = await nwPrompt(`How much did ${btn.dataset.name} attend? (optional note, e.g. 'came 3-4 times before injury')`) || '';
        if (!await nwConfirm(`Mark ${btn.dataset.name} as having attended (removes them from the ghost list)?`)) return;
        for (const id of btn.dataset.ids.split(',').filter(Boolean)) {
          await finPost(`memberships/${id}`, 'PUT', { attended_briefly: true, attendance_note: note });
        }
        loadFinanceInsights();
      }));
    }

    async function saveFinanceSettings() {
      const statusEl = document.getElementById('finance-settings-status');
      const { ok, data } = await finPost('settings', 'PUT', {
        walkins_public: document.getElementById('finance_walkins_public').checked,
        upi_id: document.getElementById('finance_upi_id').value.trim(),
        upi_name: document.getElementById('finance_upi_name').value.trim()
      });
      statusEl.textContent = ok ? 'Saved ✓' : `Error: ${data.error}`;
      // Reflect a changed UPI ID on the pay card immediately.
      if (ok) refreshUpiCard();
    }

    document.addEventListener('click', async (e) => {
      if (!e.target.classList || !e.target.classList.contains('fin-del')) return;
      const code = await nwPrompt('This permanently deletes the record. Enter the confirmation code:');
      if (!code) return;
      const kind = e.target.dataset.kind;               // e.g. 'expenses', 'memberships', 'walkins'
      const recordType = kind.endsWith('s') ? kind.slice(0, -1) : kind;
      const recordId = e.target.dataset.id;
      let ok, data;
      if (isSuperAdmin()) {
        // New triple-gated route: SuperAdmin identity + view_key + confirm
        // code, all required. Falls back to the old view_key-only route
        // below if not logged in as SuperAdmin, unchanged for now.
        const r = await authedFetch(`${API_BASE_URL}/finance-delete/${recordType}/${recordId}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ view_key: financeKey, confirm: code })
        });
        ok = r.res.ok; data = r.data || { error: r.error };
      } else {
        ({ ok, data } = await finPost(`${kind}/${recordId}`, 'DELETE', { confirm: code }));
      }
      if (!ok) { nwAlert('Error: ' + data.error); return; }
      loadFinanceExpenses(); loadFinanceMembers(); loadFinanceWalkins(); loadFinanceSummary();
    });

    async function loadPublicWalkins() {
      const el = document.getElementById('public-walkins-result');
      el.textContent = 'Loading...';
      const res = await fetch(`${API_BASE_URL}/finance/walkins/public`);
      if (res.status === 404) { el.textContent = 'The walk-in list is not enabled right now.'; return; }
      const data = await res.json();
      if (!res.ok) { el.textContent = `Error: ${data.error}`; return; }
      if (!data.walkins.length) { el.textContent = 'No walk-ins recorded.'; return; }
      let html = '<table><tr><th>Date</th><th>Slot</th><th>Guest</th></tr>';
      [...data.walkins].reverse().slice(0, 50).forEach(w => {
        html += `<tr><td>${w.date}</td><td>${w.slot}</td><td>${w.display_name}</td></tr>`;
      });
      el.innerHTML = html + '</table>';
    }

    document.getElementById('my_dues_group_select').addEventListener('change', (e) => loadMyDues(e.target.value));
    document.getElementById('finance-unlock-btn').addEventListener('click', financeUnlock);
    document.getElementById('finance_group_select').addEventListener('change', (e) => {
      currentFinanceGroupId = e.target.value || null;
      populateFinanceSlots((allGroups || []).find(g => g.group_id === currentFinanceGroupId));
      reloadFinanceForGroup();
    });
    document.getElementById('finance-load-summary-btn').addEventListener('click', loadFinanceSummary);
    document.getElementById('finance-add-expense-btn').addEventListener('click', addFinanceExpense);
    document.getElementById('finance-cancel-expense-edit-btn').addEventListener('click', resetExpenseEdit);
    document.getElementById('finance-load-expenses-btn').addEventListener('click', loadFinanceExpenses);
    document.getElementById('finance-load-members-btn').addEventListener('click', () => {
      _rememberFinance('month', document.getElementById('fmem_month').value);
      _rememberFinance('slot', document.getElementById('fmem_slot').value);
      loadFinanceMembers();
    });
    document.getElementById('finance-recalc-btn').addEventListener('click', recalcMembers);
    document.getElementById('finance-add-member-btn').addEventListener('click', addFinanceMember);
    document.getElementById('finance-copy-prev-month-btn').addEventListener('click', copyPreviousMonthMembers);
    document.getElementById('finance-bulk-add-btn').addEventListener('click', bulkAddFromRoster);
    document.getElementById('bulk-select-all-btn').addEventListener('click', () =>
      document.querySelectorAll('#bulk-roster-list input:not(:disabled)').forEach(cb => cb.checked = true));
    document.getElementById('bulk-select-none-btn').addEventListener('click', () =>
      document.querySelectorAll('#bulk-roster-list input').forEach(cb => cb.checked = false));
    (function initWalkinFilter() {
      const now = new Date();
      const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
      document.getElementById('fwalk_filter_month').value = monthNames[now.getMonth()];
      document.getElementById('fwalk_filter_year').value = now.getFullYear();
      ['fwalk_filter_month', 'fwalk_filter_year'].forEach(id =>
        document.getElementById(id).addEventListener('change', loadFinanceWalkins));
    })();
    document.getElementById('finance-add-walkin-btn').addEventListener('click', addFinanceWalkin);
    document.getElementById('finance-load-walkins-btn').addEventListener('click', loadFinanceWalkins);
    (function initInsightsFilter() {
      const now = new Date();
      const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
      document.getElementById('fins_month').value = monthNames[now.getMonth()];
      document.getElementById('fins_year').value = now.getFullYear();
      ['fins_month', 'fins_year'].forEach(id =>
        document.getElementById(id).addEventListener('change', loadFinanceInsights));
      document.getElementById('fins_estimated').addEventListener('change', renderInsights);
    })();
    document.getElementById('finance-load-insights-btn').addEventListener('click', loadFinanceInsights);
    document.getElementById('finance-save-settings-btn').addEventListener('click', saveFinanceSettings);
    document.getElementById('load-public-walkins-btn').addEventListener('click', loadPublicWalkins);
    ['fmem_month', 'fmem_year', 'fmem_slot'].forEach(id =>
      document.getElementById(id).addEventListener('change', loadFinanceMembers));
    if (financeKey && isLoggedIn()) {
      document.getElementById('finance_view_key').value = financeKey;
      financeUnlock();
    }

    // ---------- auth (Cognito login/signup/session) ----------
    // NOTE: userPool/authSession + the tiny reader functions below now
    // live at the very top of the script (right after config) - moved
    // there to fix a real bug: top-level code elsewhere (e.g. the finance
    // auto-unlock check) ran BEFORE this section during the script's
    // normal top-to-bottom execution, throwing "Cannot access authSession
    // before initialization" (a temporal-dead-zone error on the `let`).
    // Everything else auth-related (login/signup/logout flows, the modal)
    // stays here, since those only ever run from event handlers, never
    // as immediately-executing top-level code.
    // ---------- Match Review & Reorder (SuperAdmin) ----------
    let reviewMatches = [];       // the loaded day's matches, in current display order
    let reviewOriginalIds = [];   // the order as loaded, to detect a no-op apply
    let reviewLocked = false;      // a past day is settled - view only, no reorder

    async function loadReviewDay() {
      const statusEl = document.getElementById('review-status');
      const listEl = document.getElementById('review-list');
      const applyBtn = document.getElementById('review-apply-btn');
      const day = document.getElementById('review-date').value;
      if (!day) { statusEl.textContent = 'Pick a day first.'; return; }
      statusEl.textContent = 'Loading...';
      listEl.innerHTML = ''; applyBtn.style.display = 'none';

      // A day stays editable for the whole current week (Monday-start), so
      // late corrections within the week are still possible; once the week
      // has passed, its matches are settled. This also lines up with the
      // weekly scheduler, which can treat past weeks as auto-approved.
      const now = new Date();
      const dow = (now.getDay() + 6) % 7;               // 0 = Monday ... 6 = Sunday
      const monday = new Date(now);
      monday.setDate(now.getDate() - dow);
      const weekStartStr = monday.toLocaleDateString('en-CA');  // YYYY-MM-DD, local
      reviewLocked = day < weekStartStr;

      try {
        const res = await fetch(`${API_BASE_URL}/matches?date_from=${day}&date_to=${day}`);
        const data = await res.json();
        if (!res.ok) { statusEl.textContent = `Error: ${data.error}`; return; }
        reviewMatches = (data.matches || []).slice().sort((a, b) => (a.date || '').localeCompare(b.date || ''));
        reviewOriginalIds = reviewMatches.map(m => m.match_id);
        if (!reviewMatches.length) { statusEl.textContent = 'No matches that day.'; return; }
        // An approved match (stamped by the weekly scheduler once its week
        // closed) is settled regardless of the date maths - honour the flag
        // directly so the UI and the server agree.
        if (reviewMatches.some(m => m.approved)) reviewLocked = true;
        if (reviewLocked) {
          statusEl.textContent = `${reviewMatches.length} matches - this was before the current week and is settled, so it can't be reordered.`;
        } else if (reviewMatches.length < 2) {
          statusEl.textContent = 'Only one match today - nothing to reorder.';
        } else {
          statusEl.textContent = `${reviewMatches.length} matches. Hold and drag a row to slide it into place, then apply.`;
          applyBtn.style.display = 'inline-block';
        }
        renderReviewList();
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    // Whether the current visual order differs from what was loaded.
    function reviewOrderChanged() {
      const now = reviewMatches.map(m => m.match_id);
      return now.length !== reviewOriginalIds.length
        || now.some((id, i) => id !== reviewOriginalIds[i]);
    }

    function renderReviewList() {
      const listEl = document.getElementById('review-list');
      listEl.innerHTML = '';
      const changed = reviewOrderChanged();
      reviewMatches.forEach((m, idx) => {
        const li = document.createElement('li');
        li.draggable = !reviewLocked;
        li.dataset.idx = idx;
        li.style.cssText = `display:flex; align-items:center; gap:10px; padding:10px 12px; margin:6px 0; border:1px solid var(--border); border-radius:8px; background:var(--surface-2); ${reviewLocked ? '' : 'cursor:grab;'}`;
        const time = (m.date || '').slice(11, 19) || '';
        const teamA = playerLabelsById(m.team_a, m.team_a_names).join(' & ');
        const teamB = playerLabelsById(m.team_b, m.team_b_names).join(' & ');
        // A live position number, so the reordering is legible at a glance
        // rather than having to read timestamps.
        li.innerHTML =
          `<span style="min-width:22px; height:22px; border-radius:50%; background:var(--court); color:#fff; font-size:12px; font-weight:700; display:flex; align-items:center; justify-content:center;">${idx + 1}</span>
           <span style="opacity:0.5; font-size:12px; min-width:64px;">${time}</span>
           <span style="flex:1;">${escapeHtml(teamA)} <strong>${m.score_a}-${m.score_b}</strong> ${escapeHtml(teamB)}</span>
           ${reviewLocked ? '' : '<span style="opacity:0.4;">⠿</span>'}`;

        if (!reviewLocked) {
          li.addEventListener('dragstart', e => {
            // getData() is unreadable during dragover (spec puts it in
            // protected mode until drop), so stash the source index in a var
            // too - the dragover handler relies on this to know what's moving.
            reviewDragFrom = idx;
            e.dataTransfer.setData('text/plain', idx);
            e.dataTransfer.effectAllowed = 'move';
            li.style.opacity = '0.4';
          });
          li.addEventListener('dragend', () => { li.style.opacity = ''; });
          li.addEventListener('dragover', e => {
            e.preventDefault();               // allow the drop
            e.dataTransfer.dropEffect = 'move';
            if (reviewDragFrom !== idx) li.style.borderTop = '2px solid var(--court)';
          });
          li.addEventListener('dragleave', () => { li.style.borderTop = ''; });
          li.addEventListener('drop', e => {
            e.preventDefault();
            li.style.borderTop = '';
            const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
            const src = isNaN(from) ? reviewDragFrom : from;
            const to = idx;
            if (src == null || isNaN(src) || src === to) return;
            const [moved] = reviewMatches.splice(src, 1);
            reviewMatches.splice(to, 0, moved);
            reviewDragFrom = null;
            renderReviewList();               // single rebuild AFTER the drop
          });
        }
        listEl.appendChild(li);
      });

      // Reflect whether there's anything to apply on the button itself.
      const applyBtn = document.getElementById('review-apply-btn');
      if (applyBtn && !reviewLocked) {
        applyBtn.textContent = changed ? 'Apply new order & recompute' : 'Confirm order (no change)';
      }
    }
    let reviewDragFrom = null;  // index of the row currently being dragged

    async function applyReviewOrder() {
      const statusEl = document.getElementById('review-status');

      // No change -> nothing to recompute. Just acknowledge; don't burn a
      // full rating replay for an order that's already correct.
      if (!reviewOrderChanged()) {
        statusEl.textContent = 'Order confirmed - no change, so nothing was recomputed.';
        return;
      }

      if (!await nwConfirm('Apply this order?\n\nThe timestamps for this day\'s matches will be swapped to match, and every player\'s rating recomputed from scratch. This cannot be undone automatically.')) return;
      statusEl.textContent = 'Applying and recomputing...';
      try {
        const { res, error } = await authedFetch(`${API_BASE_URL}/reorder-matches`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ match_ids: reviewMatches.map(m => m.match_id) })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }
        statusEl.textContent = 'Done - order applied and ratings recomputed.';
        loadReviewDay();  // reload to show the corrected times and reset the baseline
      } catch (e) { statusEl.textContent = `Failed: ${e.message}`; }
    }

    document.getElementById('review-load-btn').addEventListener('click', loadReviewDay);
    document.getElementById('review-apply-btn').addEventListener('click', applyReviewOrder);


    function updateAuthUI() {
      const statusEl = document.getElementById('auth-status');
      const loginBtn = document.getElementById('auth-login-btn');
      const logoutBtn = document.getElementById('auth-logout-btn');

      // The reorder tool rewrites rating history, so it's SuperAdmin-only -
      // hidden entirely for everyone else rather than just disabled.
      const reviewBtn = document.getElementById('review-tab-btn');
      if (reviewBtn) reviewBtn.style.display = canReviewRequests() ? '' : 'none';
      updateReviewTabScope();
      const storeBtn = document.getElementById('store-tab-btn');
      if (storeBtn) storeBtn.style.display = xpVisible() ? '' : 'none';
      // If the tab you're currently ON just became unavailable (e.g. you were
      // in Reviews and logged out), don't leave its panel showing - fall back
      // to Players. Otherwise a logged-out user keeps seeing an admin panel
      // until they refresh.
      const activePanel = document.querySelector('.tab-panel.active');
      const hiddenNow = (id, btn) => activePanel && activePanel.id === id && btn && btn.style.display === 'none';
      if (hiddenNow('tab-review', reviewBtn) || hiddenNow('tab-store', storeBtn)) {
        const playersBtn = document.querySelector('.tab-btn[data-tab="players"]');
        if (playersBtn) playersBtn.click();
      }
      if (typeof updateHeaderCoins === 'function') updateHeaderCoins();

      // Epic 7: guests see a login prompt instead of the match/tournament
      // creation forms - genuinely enforced server-side too, this is just
      // the friendly version of that (no filling out a form only to hit
      // a 403 at the end).
      // Three states now, not two. "Guest" (no session at all) and
      // "logged in but not linked to a player" are different problems
      // with different fixes, and showing the guest's Log-in button to
      // someone who is already logged in is exactly the dead end that
      // came up during the last session.
      const loggedIn = isLoggedIn();
      const linked = hasLinkedPlayer();
      const showForms = loggedIn && linked;
      document.getElementById('register-guest-notice').style.display = loggedIn ? 'none' : 'block';
      document.getElementById('register-player-card').style.display = loggedIn ? 'block' : 'none';
      document.getElementById('record-match-guest-notice').style.display = loggedIn ? 'none' : 'block';
      document.getElementById('record-match-unlinked-notice').style.display = (loggedIn && !linked) ? 'block' : 'none';
      document.getElementById('record-match-card').style.display = showForms ? 'block' : 'none';
      document.getElementById('match-quick-add-player-btn').style.display = showForms ? 'inline-block' : 'none';
      document.getElementById('create-tournament-guest-notice').style.display = showForms ? 'none' : 'block';
      document.getElementById('create-tournament-card').style.display = showForms ? 'block' : 'none';
      // Guests see only the public UPI QR card, not even the option to
      // try entering a finance view key.
      // Don't reshow the key form if finance is already unlocked - a
      // background updateAuthUI (e.g. token refresh) would otherwise pop
      // the lock card back over the loaded data.
      const financeUnlocked = document.getElementById('finance-content').style.display === 'block';
      document.getElementById('finance-lock-card').style.display = (showForms && !financeUnlocked) ? 'block' : 'none';
      if (!showForms) document.getElementById('finance-content').style.display = 'none';
      // Profile: guests see a login prompt only; logged-in users see the
      // real tab (populated from /visible-players, group-scoped server-side).
      document.getElementById('profile-guest-notice').style.display = showForms ? 'none' : 'block';
      document.getElementById('profile-content-wrapper').style.display = showForms ? 'block' : 'none';
      if (showForms) loadVisiblePlayers();
      document.getElementById('register-group-link-label').style.display = showForms ? 'block' : 'none';
      if (authSession) {
        const linkedPlayer = allPlayers.find(p => p.player_id === myPlayerId());
        const identity = linkedPlayer
          ? formatPlayerLabel(linkedPlayer.name, linkedPlayer.nickname)
          : `${authSession.claims.email} (no profile linked)`;
        statusEl.textContent = identity + (isSuperAdmin() ? ' (SuperAdmin)' : '');
        loginBtn.style.display = 'none';
        logoutBtn.style.display = 'inline-block';
        // A pure SuperAdmin account (no player of its own) still needs
        // Settings, because claim approvals live in there. Gating this on
        // linkedPlayer alone would lock the oversight account out of the
        // one screen it exists to use.
        document.getElementById('open-settings-btn').style.display =
          (linkedPlayer || isSuperAdmin()) ? 'inline-block' : 'none';
      } else {
        statusEl.textContent = 'Guest';
        loginBtn.style.display = 'inline-block';
        logoutBtn.style.display = 'none';
        document.getElementById('open-settings-btn').style.display = 'none';
      }
      if (typeof loadGroupMembers === 'function') {
        const sel = document.getElementById('group_select');
        if (sel && sel.value) loadGroupMembers(sel.value);
      }
      // Logging in, logging out, and a roster refresh all change what
      // "my own background" resolves to, and all three land here.
      updatePageBackground();
    }

    function openAuthModal() { document.getElementById('auth-modal').style.display = 'flex'; showAuthView('login'); }
    function closeAuthModal() { document.getElementById('auth-modal').style.display = 'none'; }
    function showAuthView(view) {
      ['login', 'newpassword', 'signup', 'confirm', 'forgot'].forEach(v => {
        document.getElementById(`auth-${v}-view`).style.display = (v === view) ? 'block' : 'none';
      });
      document.getElementById('auth-modal-title').textContent =
        { login: 'Log in', newpassword: 'Set new password', signup: 'Sign up', confirm: 'Confirm sign-up', forgot: 'Reset password' }[view];
    }

    function setAuthSession(session, user, opts = {}) {
      const idToken = session.getIdToken();
      authSession = { idToken: idToken.getJwtToken(), claims: idToken.payload, cognitoUser: user };
      updateAuthUI();
      closeAuthModal();
      // First login on an account with no linked player yet - prompt to
      // create one, right here rather than tying it to signup
      // specifically (covers self-signup and any other unlinked-account
      // case identically). Skipped on a silent restore.
      if (!opts.silent && !authSession.claims['custom:player_id']) {
        openCompleteProfileModal();
      }
      // If a match was stranded before a re-login, surface it now so they
      // can finish it with one tap.
      offerPendingMatchRestore();
    }

    async function openCompleteProfileModal() {
      document.getElementById('complete-profile-modal').style.display = 'flex';
      // If there's nothing to claim, the chooser is a pointless extra step -
      // send them straight to create-new. Otherwise show the chooser so a
      // returning player can claim their existing history.
      try {
        const res = await fetch(`${API_BASE_URL}/players`);
        const data = await res.json();
        const hasClaimable = (data.players || []).some(p => !p.claimed);
        showCompleteProfileMode(hasClaimable ? 'chooser' : 'create');
      } catch (_) {
        showCompleteProfileMode('chooser');  // on error, don't hide the option
      }
    }

    function showCompleteProfileMode(mode, preselectPlayerId) {
      document.getElementById('complete-profile-chooser').style.display = mode === 'chooser' ? 'block' : 'none';
      document.getElementById('complete-profile-create-view').style.display = mode === 'create' ? 'block' : 'none';
      document.getElementById('complete-profile-claim-view').style.display = mode === 'claim' ? 'block' : 'none';
      document.getElementById('complete-profile-status').textContent = '';
      if (mode === 'claim') populateClaimPicker(preselectPlayerId);
    }

    async function populateClaimPicker(preselectPlayerId) {
      const select = document.getElementById('complete-profile-claim-select');
      select.innerHTML = '<option value="">Loading...</option>';
      try {
        const res = await fetch(`${API_BASE_URL}/players`);
        const data = await res.json();
        const unclaimed = (data.players || []).filter(p => !p.claimed);
        if (!unclaimed.length) {
          select.innerHTML = '<option value="">No unclaimed profiles found</option>';
          return;
        }
        const labeled = unclaimed.map(p => ({ ...p, label: `${p.name} (${p.nickname})` }));
        populateSelect(select, labeled, 'player_id', 'label', null);
        // Arriving here from a nickname-collision prompt: land directly
        // on the player we think you are, rather than making you hunt
        // for them again in a list you already answered a question about.
        if (preselectPlayerId && unclaimed.some(p => p.player_id === preselectPlayerId)) {
          select.value = preselectPlayerId;
        }
      } catch (err) {
        select.innerHTML = '<option value="">Failed to load</option>';
      }
    }

    async function submitClaimProfile() {
      const playerId = document.getElementById('complete-profile-claim-select').value;
      const confirm = document.getElementById('complete-profile-claim-code').value.trim();
      const statusEl = document.getElementById('complete-profile-status');
      if (!playerId) { statusEl.textContent = 'Pick which player is you.'; return; }
      if (!confirm) { statusEl.textContent = 'Confirmation code is required.'; return; }
      statusEl.textContent = 'Claiming your profile...';

      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/claim-player`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ player_id: playerId, confirm })
        });
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }

        // Same self-service linking dance as the create-new path.
        const cognitoUser = authSession.cognitoUser;
        if (!cognitoUser) { statusEl.textContent = 'Claimed, but linking failed - please log out and back in.'; return; }
        cognitoUser.updateAttributes(
          [new AmazonCognitoIdentity.CognitoUserAttribute({ Name: 'custom:player_id', Value: data.player_id })],
          (err) => {
            if (err) { statusEl.textContent = `Claimed, but linking failed: ${err.message}`; return; }
            cognitoUser.getSession((sessionErr, session) => {
              if (!sessionErr && session) {
                cognitoUser.refreshSession(session.getRefreshToken(), (refreshErr, newSession) => {
                  if (!refreshErr && newSession) { setAuthSession(newSession, cognitoUser); }
                });
              }
              closeCompleteProfileModal();
              loadPlayers();
              loadGroups();
            });
          }
        );
      } catch (err) {
        statusEl.textContent = `Request failed: ${err.message}`;
      }
    }

    function closeCompleteProfileModal() {
      document.getElementById('complete-profile-modal').style.display = 'none';
    }

    /** Same rule the two backend Lambdas apply, mirrored client-side so
     *  what we compare against is what would actually be stored. */
    function sanitizeNickname(raw) {
      return (raw || '').toLowerCase().replace(/[^a-z0-9_]/g, '');
    }

    /** Classic Levenshtein - small strings only, so the naive version is fine. */
    function editDistance(a, b) {
      const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
      for (let j = 0; j <= b.length; j++) dp[0][j] = j;
      for (let i = 1; i <= a.length; i++) {
        for (let j = 1; j <= b.length; j++) {
          dp[i][j] = a[i - 1] === b[j - 1]
            ? dp[i - 1][j - 1]
            : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
        }
      }
      return dp[a.length][b.length];
    }

    /**
     * Returns true if it's safe to create a NEW player, false if the flow
     * was diverted into claiming an existing one (or cancelled).
     *
     * Three tiers, deliberately different in how hard they push:
     *   exact nickname match -> hard stop, you cannot create a duplicate
     *   near nickname / same real name -> ask, defaulting to "that's me"
     *   nothing similar -> straight through, no friction
     */
    async function checkForExistingPlayer(name, typedNickname, statusEl) {
      statusEl.textContent = 'Checking for an existing profile...';
      let players = [];
      try {
        const res = await fetch(`${API_BASE_URL}/players`);
        const data = await res.json();
        players = data.players || [];
      } catch (err) {
        // Never block registration because a lookup failed - the backend
        // still enforces exact-nickname uniqueness as a backstop.
        return true;
      }
      statusEl.textContent = '';

      // If no nickname was typed, the backend derives one from the real
      // name - so check what WOULD be generated, not an empty string.
      const candidate = sanitizeNickname(typedNickname) || sanitizeNickname(name);
      if (!candidate) return true;

      const exact = players.find(p => sanitizeNickname(p.nickname) === candidate);
      if (exact) {
        const claimable = !exact.claimed;
        const msg = `The nickname "${candidate}" already belongs to ${exact.name}.\n\n` +
          (claimable
            ? 'If that is you, link this login to that existing profile so your match history and rating stay in one place.\n\nOK = link me to that profile\nCancel = go back and pick a different nickname'
            : 'That profile is already linked to another account, so you will need a different nickname.');
        if (!claimable) { nwAlert(msg); return false; }
        if (await nwConfirm(msg)) { showCompleteProfileMode('claim', exact.player_id); return false; }
        return false;  // either way, do NOT create a duplicate under a colliding nickname
      }

      // Near misses: shortenings ("maya" for "mayank"), typos, or the
      // exact same real name already on the roster.
      const nameNorm = name.trim().toLowerCase();
      const near = players.filter(p => {
        if (p.claimed) return false;
        const nk = sanitizeNickname(p.nickname);
        if (!nk) return false;
        if (nk.startsWith(candidate) || candidate.startsWith(nk)) return true;
        if (editDistance(nk, candidate) <= 2) return true;
        return (p.name || '').trim().toLowerCase() === nameNorm;
      });
      if (!near.length) return true;

      const list = near.slice(0, 5).map(p => `  - ${p.name} (${p.nickname})`).join('\n');
      const answer = await nwConfirm(
        `We already have ${near.length === 1 ? 'a player' : 'players'} who might be you:\n\n${list}\n\n` +
        'Creating a second profile splits your ratings and match history in two.\n\n' +
        'OK = one of these is me, link my account to it\n' +
        'Cancel = these are other people, create me a new profile'
      );
      if (answer) { showCompleteProfileMode('claim', near[0].player_id); return false; }
      return true;
    }

    async function submitCompleteProfile() {
      const name = document.getElementById('complete-profile-name').value.trim();
      const nickname = document.getElementById('complete-profile-nickname').value.trim();
      const statusEl = document.getElementById('complete-profile-status');
      if (!name) { statusEl.textContent = 'Name is required.'; return; }

      // Duplicate-person check BEFORE creating anything. The backend only
      // rejects an exact nickname collision, which is no help in the case
      // that actually happened: an existing player "mayank" and a signup
      // typing "maya". Different strings, no collision, brand new second
      // profile for the same human - and all their history split in two.
      if (!nickname) { statusEl.textContent = 'Nickname is required.'; return; }

      const proceed = await checkForExistingPlayer(name, nickname, statusEl);
      if (!proceed) return;

      statusEl.textContent = 'Creating your profile...';

      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/action-request`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'new_profile', name, nickname })
        });

        // 409 = the nickname already exists as an unclaimed profile, which
        // is probably this same person. Offer to claim it instead of
        // creating a duplicate history.
        if (res.status === 409 && data && data.suggest_claim_player_id) {
          statusEl.textContent = '';
          const claimIt = await nwConfirm(`${data.error}\n\nClaim that existing profile instead?`);
          if (claimIt) { showCompleteProfileMode('claim', data.suggest_claim_player_id); }
          return;
        }
        if (!res.ok) { statusEl.textContent = `Error: ${error}`; return; }

        // Instant create: the profile is made and linked to this account
        // server-side, so link the token and log straight in - no approval,
        // no sign-out. A brand-new name needs no admin check.
        if (data && data.linked && data.player_id) {
          statusEl.textContent = 'Profile created - logging you in...';
          const cognitoUser = authSession.cognitoUser;
          if (!cognitoUser) { location.reload(); return; }
          cognitoUser.updateAttributes(
            [new AmazonCognitoIdentity.CognitoUserAttribute({ Name: 'custom:player_id', Value: data.player_id })],
            (err) => {
              if (err) { statusEl.textContent = `Created, but linking failed: ${err.message}. Try logging out and back in.`; return; }
              cognitoUser.getSession((sessionErr, session) => {
                if (!sessionErr && session) {
                  cognitoUser.refreshSession(session.getRefreshToken(), (refreshErr, newSession) => {
                    if (!refreshErr && newSession) {
                      setAuthSession(newSession, cognitoUser, { silent: true });
                      closeCompleteProfileModal();
                      location.reload();  // fresh page as the newly-linked player
                    } else { location.reload(); }
                  });
                } else { location.reload(); }
              });
            });
          return;
        }
        // Fallback (shouldn't happen now): treat as a request.
        finishRequestAndSignOut(`Thanks ${name} - your profile request has been sent to the admin.`);
        return;
      } catch (err) {
        statusEl.textContent = `Request failed: ${err.message}`;
      }
    }

    /**
     * A pending request leaves the account in limbo: logged in, but unable
     * to do anything until an admin acts. Leaving people sitting in that
     * state made them think it had failed, so they'd try again or get
     * stuck. Signing them back out to guest makes the state honest - come
     * back and log in once it's approved.
     */
    function finishRequestAndSignOut(message) {
      closeCompleteProfileModal();
      doLogout();
      nwAlert(`${message}\n\nYou'll be able to log in properly once it's approved. If it's rejected, you can sign up again with the same email.`);
    }

    async function doLogin() {
      const identifier = document.getElementById('auth-login-email').value.trim();
      const password = document.getElementById('auth-login-password').value;
      const statusEl = document.getElementById('auth-login-status');
      if (!userPool) { statusEl.textContent = 'Login is not configured yet.'; return; }

      let email = identifier;
      if (!identifier.includes('@')) {
        statusEl.textContent = 'Looking up your account...';
        try {
          const res = await fetch(`${API_BASE_URL}/players?login_identifier=${encodeURIComponent(identifier)}`);
          const data = await res.json();
          if (!res.ok) { statusEl.textContent = 'No account found for that name - try your email instead, or check the spelling.'; return; }
          email = data.email;
        } catch (err) {
          statusEl.textContent = 'Lookup failed - try your email instead.';
          return;
        }
      }
      statusEl.textContent = '';

      const user = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
      const authDetails = new AmazonCognitoIdentity.AuthenticationDetails({ Username: email, Password: password });
      window._pendingAuthUser = user;
      user.authenticateUser(authDetails, {
        onSuccess: (session) => { window._pendingAuthUser = null; setAuthSession(session, user); },
        onFailure: (err) => {
          // Stuck-onboarding recovery: the account exists in Cognito but its
          // email was never verified (e.g. they signed up, then closed the
          // site before entering the code). Login can never succeed for them,
          // and there was previously no way back to the code screen. Send a
          // fresh code and drop them straight onto the confirm view - stashing
          // the password so confirmation can auto-log them in and open the
          // profile/claim chooser, exactly like a first-time signup would.
          if (err && err.code === 'UserNotConfirmedException') {
            statusEl.textContent = 'Your email was never verified - sending a fresh code...';
            window._pendingSignupPassword = password;
            document.getElementById('auth-confirm-code').dataset.email = email;
            const cu = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
            cu.resendConfirmationCode((rErr) => {
              const cs = document.getElementById('auth-confirm-status');
              if (rErr) { statusEl.textContent = rErr.message; return; }
              showAuthView('confirm');
              if (cs) cs.textContent = 'We sent a new code to ' + email + '. Enter it to finish signing up.';
            });
            return;
          }
          statusEl.textContent = err.message;
        },
        newPasswordRequired: () => { showAuthView('newpassword'); }
      });
    }

    function doNewPassword() {
      const user = window._pendingAuthUser;
      const statusEl = document.getElementById('auth-newpassword-status');
      if (!user) { statusEl.textContent = 'Session expired, log in again.'; showAuthView('login'); return; }
      const newPassword = document.getElementById('auth-newpassword-input').value;
      user.completeNewPasswordChallenge(newPassword, {}, {
        onSuccess: (session) => { setAuthSession(session, user); },
        onFailure: (err) => { statusEl.textContent = err.message; }
      });
    }

    function doSignup() {
      const email = document.getElementById('auth-signup-email').value.trim();
      const password = document.getElementById('auth-signup-password').value;
      const statusEl = document.getElementById('auth-signup-status');
      if (!userPool) { statusEl.textContent = 'Sign up is not configured yet.'; return; }
      const attrs = [new AmazonCognitoIdentity.CognitoUserAttribute({ Name: 'email', Value: email })];
      userPool.signUp(email, password, attrs, null, (err) => {
        if (err) { statusEl.textContent = err.message; return; }
        document.getElementById('auth-confirm-code').dataset.email = email;
        // Stash the password briefly so confirmation can log them straight
        // in and into the profile chooser, rather than stranding them at a
        // login screen (which is where the onboarding flow was breaking).
        window._pendingSignupPassword = password;
        showAuthView('confirm');
      });
    }

    function doConfirmSignup() {
      const email = document.getElementById('auth-confirm-code').dataset.email;
      const code = document.getElementById('auth-confirm-code').value.trim();
      const statusEl = document.getElementById('auth-confirm-status');
      const user = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
      user.confirmRegistration(code, true, (err) => {
        if (err) { statusEl.textContent = err.message; return; }
        // Log them straight in (using the password from signup) so the
        // profile chooser opens automatically via setAuthSession's
        // unlinked-account check. Falls back to the login screen if the
        // password isn't around (e.g. they reloaded between steps).
        const pw = window._pendingSignupPassword;
        window._pendingSignupPassword = null;
        if (pw) {
          statusEl.textContent = 'Confirmed! Logging you in...';
          const authDetails = new AmazonCognitoIdentity.AuthenticationDetails({ Username: email, Password: pw });
          const loginUser = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
          loginUser.authenticateUser(authDetails, {
            onSuccess: (session) => { setAuthSession(session, loginUser); },
            onFailure: () => { statusEl.textContent = 'Confirmed! You can log in now.'; setTimeout(() => showAuthView('login'), 1000); }
          });
        } else {
          statusEl.textContent = 'Confirmed! You can log in now.';
          setTimeout(() => showAuthView('login'), 1200);
        }
      });
    }

    // Manual "resend code" from the confirm screen, for anyone who lost the
    // original email or let the code expire. Uses the email stashed on the
    // confirm-code field (set by signup or by the login recovery path above).
    function doResendConfirmCode() {
      const email = document.getElementById('auth-confirm-code').dataset.email;
      const statusEl = document.getElementById('auth-confirm-status');
      if (!email) { statusEl.textContent = 'Start from the Log in or Sign up screen so we know your email.'; return; }
      if (!userPool) { statusEl.textContent = 'Sign up is not configured yet.'; return; }
      const user = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
      user.resendConfirmationCode((err) => {
        statusEl.textContent = err ? err.message : ('New code sent to ' + email + '.');
      });
    }

    function doForgotPassword() {
      const email = document.getElementById('auth-forgot-email').value.trim();
      const statusEl = document.getElementById('auth-forgot-status');
      const user = new AmazonCognitoIdentity.CognitoUser({ Username: email, Pool: userPool });
      window._pendingAuthUser = user;
      user.forgotPassword({
        onSuccess: () => { statusEl.textContent = 'Reset code sent.'; },
        onFailure: (err) => { statusEl.textContent = err.message; },
        inputVerificationCode: () => {
          statusEl.textContent = 'Reset code sent - enter it below.';
          document.getElementById('auth-forgot-confirm-block').style.display = 'block';
        }
      });
    }

    function doConfirmForgotPassword() {
      const user = window._pendingAuthUser;
      const statusEl = document.getElementById('auth-forgot-status');
      if (!user) { statusEl.textContent = 'Session expired, start again.'; return; }
      const code = document.getElementById('auth-forgot-code').value.trim();
      const newPassword = document.getElementById('auth-forgot-newpassword').value;
      user.confirmPassword(code, newPassword, {
        onSuccess: () => { statusEl.textContent = 'Password reset - log in now.'; setTimeout(() => showAuthView('login'), 1200); },
        onFailure: (err) => { statusEl.textContent = err.message; }
      });
    }

    function doLogout() {
      // signOut() matters: without it the Cognito SDK leaves this user's
      // refresh token sitting in localStorage, so getCurrentUser() keeps
      // returning them after logout - on a shared phone that is somebody
      // else's session waiting to be picked back up.
      try {
        const user = (authSession && authSession.cognitoUser) || (userPool && userPool.getCurrentUser());
        if (user) user.signOut();
      } catch (e) { /* signing out of an already-dead session is not an error */ }

      authSession = null;
      sessionStorage.removeItem('nw_auth_token');  // legacy key from the old hand-rolled restore - cleared so stale copies can't linger

      // The DOM outlives the session. Anything scoped to the person who
      // just left has to be cleared explicitly or the next login inherits
      // it - which is exactly how one account ended up showing another
      // account's Player Card by default.
      profileSelectionOwner = null;
      ['profile_player_select', 'profile_h2h_opponent_select', 'profile_partner_select',
       'profile_compare2_select', 'profile_compare3_select', 'profile_compare4_select']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
      renderProfileCardBanner(null);

      updateAuthUI();
    }

    /**
     * Sessions used to be restored from a token kept in sessionStorage,
     * which is per-tab and destroyed the moment the tab closes - so
     * closing the tab and reopening a bookmark always landed on "Guest",
     * and even reopening in the same tab after an hour did too, because
     * the stored token had simply expired with nothing to renew it.
     *
     * The Cognito SDK already persists to localStorage and holds a
     * refresh token good for ~30 days, so getCurrentUser() survives tab
     * close and browser restart, and getSession() silently mints a fresh
     * ID token from it. Letting the SDK own this removes the hand-rolled
     * copy entirely rather than fixing it in two places.
     *
     * getSession is callback-based, so the very first paint may briefly
     * read "Guest" before this resolves - updateAuthUI() runs again on
     * completion, and loadPlayers() is re-run so anything gated on
     * identity renders with the restored session.
     */
    function restoreSession() {
      if (!userPool) return;
      const user = userPool.getCurrentUser();
      if (!user) return;
      user.getSession((err, session) => {
        if (err || !session || !session.isValid()) return;
        // silent: a page load is not the moment to throw a modal at
        // someone. The unlinked-account notice already tells them what
        // to do, and it doesn't block the rest of the page.
        setAuthSession(session, user, { silent: true });
        loadPlayers();
      });
    }

    document.getElementById('auth-login-btn').addEventListener('click', openAuthModal);
    document.getElementById('auth-logout-btn').addEventListener('click', doLogout);
    document.getElementById('auth-login-submit-btn').addEventListener('click', doLogin);
    document.getElementById('auth-newpassword-submit-btn').addEventListener('click', doNewPassword);
    document.getElementById('auth-signup-submit-btn').addEventListener('click', doSignup);
    document.getElementById('auth-confirm-submit-btn').addEventListener('click', doConfirmSignup);
    document.getElementById('auth-confirm-resend-btn').addEventListener('click', doResendConfirmCode);
    document.getElementById('auth-forgot-submit-btn').addEventListener('click', doForgotPassword);
    document.getElementById('auth-forgot-confirm-btn').addEventListener('click', doConfirmForgotPassword);
    document.getElementById('complete-profile-submit-btn').addEventListener('click', submitCompleteProfile);
    document.getElementById('complete-profile-claim-btn').addEventListener('click', submitClaimProfile);
    document.getElementById('complete-profile-request-btn').addEventListener('click', submitClaimRequest);
    document.getElementById('claim-code-toggle').addEventListener('click', (e) => {
      e.preventDefault();
      const fields = document.getElementById('claim-code-fields');
      fields.style.display = fields.style.display === 'block' ? 'none' : 'block';
    });

    /** Restore the tab named in the URL on load. Guarded against a stale
     *  or hand-edited hash naming a tab that no longer exists. */
    function restoreTabFromHash() {
      const wanted = (location.hash || '').replace('#', '');
      if (!wanted) return;
      const btn = document.querySelector(`.tab-btn[data-tab="${CSS.escape(wanted)}"]`);
      if (btn) btn.click();
    }
    restoreTabFromHash();

    restoreSession();

    // A stranded match should surface even before login (e.g. the session
    // died, they reloaded, and are logged out) so it's never silently lost.
    offerPendingMatchRestore();
    updateAuthUI();

    // ---------- init ----------

    (async () => {
      await loadPlayers();
      await loadGroups();
      refreshEventBanner();
      // Learn whether the admin has made the gamification UI public, so
      // non-admins see levels/coins/store/quests when it's enabled.
      try {
        const asRes = await fetch(`${API_BASE_URL}/app-settings`);
        if (asRes.ok) { const _as = await asRes.json(); xpPublic = !!_as.xp_public; voiceEnabled = !!_as.voice_enabled; if (typeof applyVoiceVisibility === 'function') applyVoiceVisibility(); }
      } catch (_) {}
      if (typeof updateAuthUI === 'function') updateAuthUI();
      loadTournamentGroupOptions();
      loadTournamentsList();
      loadRankings();
      loadDiversity();
      loadBadges();
      loadHistory();
      loadHallOfFame();
      loadAttendance();
      loadPublicWalkins();
      loadProfile();
    })();

    // ---------- tournaments ----------

    document.getElementById('tournament_format').addEventListener('change', (e) => {
      document.getElementById('subgroup-options').style.display = e.target.value === 'groups_then_knockout' ? 'block' : 'none';
    });

    document.getElementById('tournament_pairing_mode').addEventListener('change', (e) => {
      const isManual = e.target.value === 'manual';
      document.getElementById('manual-teams-section').style.display = isManual ? 'block' : 'none';
      document.getElementById('tournament-participants-section').style.display = isManual ? 'none' : 'block';
      if (isManual && document.getElementById('manual-teams-container').children.length === 0) {
        addManualTeamRow();
        addManualTeamRow();
      }
    });

    document.getElementById('tournament_match_type').addEventListener('change', () => {
      // rebuild existing manual team rows if match type (singles/doubles) changes
      const container = document.getElementById('manual-teams-container');
      const count = container.children.length;
      container.innerHTML = '';
      for (let i = 0; i < count; i++) addManualTeamRow();
    });

    document.getElementById('add-manual-team-btn').addEventListener('click', () => addManualTeamRow());

    function addManualTeamRow() {
      const container = document.getElementById('manual-teams-container');
      const isDoubles = document.getElementById('tournament_match_type').value === 'doubles';
      const rowIndex = container.children.length;
      const labeled = allPlayers.map(p => ({ ...p, label: `${p.name} (${p.nickname}) (${p.rating})` }));

      const row = document.createElement('div');
      row.className = 'row';
      row.style.marginTop = '6px';

      const sel1 = document.createElement('select');
      sel1.id = `manual_team_${rowIndex}_p1`;
      const div1 = document.createElement('div');
      div1.innerHTML = `<strong>Team ${rowIndex + 1}</strong>`;
      div1.appendChild(sel1);
      row.appendChild(div1);

      if (isDoubles) {
        const sel2 = document.createElement('select');
        sel2.id = `manual_team_${rowIndex}_p2`;
        const div2 = document.createElement('div');
        div2.appendChild(sel2);
        row.appendChild(div2);
        populateSelect(sel2, labeled, 'player_id', 'label', null);
      }

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.textContent = 'Remove';
      removeBtn.onclick = () => row.remove();
      row.appendChild(removeBtn);

      populateSelect(sel1, labeled, 'player_id', 'label', null);
      container.appendChild(row);
    }

    function collectManualTeams() {
      const container = document.getElementById('manual-teams-container');
      const isDoubles = document.getElementById('tournament_match_type').value === 'doubles';
      const teams = [];
      for (const row of container.children) {
        const selects = row.querySelectorAll('select');
        const ids = Array.from(selects).map(s => s.value).filter(Boolean);
        if (isDoubles && ids.length === 2) teams.push(ids);
        else if (!isDoubles && ids.length === 1) teams.push(ids);
      }
      return teams;
    }

    async function loadTournamentGroupOptions() {
      populateSelect(document.getElementById('tournament_group_select'), allGroups, 'group_id', 'group_name', null);
      if (allGroups.length) {
        loadTournamentParticipantsChecklist();
      }
    }

    document.getElementById('tournament_group_select').addEventListener('change', loadTournamentParticipantsChecklist);

    async function loadTournamentParticipantsChecklist() {
      const groupId = document.getElementById('tournament_group_select').value;
      const container = document.getElementById('tournament-participants-checklist');
      if (!groupId) { container.innerHTML = ''; updateParticipantsCount(); return; }

      container.innerHTML = 'Loading...';
      const res = await fetch(`${API_BASE_URL}/groups/${groupId}`);
      const data = await res.json();
      if (!res.ok) {
        container.innerHTML = '<p style="font-size:13px;color:#555;margin:0;">Could not load this group.</p>';
        updateParticipantsCount();
        return;
      }
      applyGroupDefaultsToForm('tournament_', data.default_tournament_settings);
      if (!data.members || !data.members.length) {
        container.innerHTML = '<p style="font-size:13px;color:#555;margin:0;">No members in this group yet.</p>';
        updateParticipantsCount();
        return;
      }
      container.innerHTML = [...data.members].sort((a, b) => a.name.localeCompare(b.name)).map(m =>
        `<label style="display:block; padding:2px 0;"><input type="checkbox" class="tournament-participant-checkbox" value="${m.player_id}" checked> ${m.name} (${m.rating})</label>`
      ).join('');
      updateParticipantsCount();
    }

    document.getElementById('tournament-participants-checklist').addEventListener('change', (e) => {
      if (e.target.classList.contains('tournament-participant-checkbox')) updateParticipantsCount();
    });
    document.getElementById('tournament_match_type').addEventListener('change', updateParticipantsCount);

    function updateParticipantsCount() {
      const countEl = document.getElementById('tournament-participants-count');
      const count = collectTournamentParticipants().length;
      const matchType = document.getElementById('tournament_match_type').value;
      if (!count) { countEl.textContent = ''; return; }
      let msg = `${count} player${count === 1 ? '' : 's'} selected`;
      if (matchType === 'doubles' && count % 2 === 1) {
        msg += ' - odd number, you\'ll need a filler for doubles';
      }
      countEl.textContent = msg;
    }

    function collectTournamentParticipants() {
      return Array.from(document.querySelectorAll('.tournament-participant-checkbox:checked')).map(cb => cb.value);
    }

    async function loadTournamentsList() {
      const res = await fetch(`${API_BASE_URL}/tournaments`);
      const data = await res.json();
      const items = (data.tournaments || []).map(t => ({ ...t, label: `${t.name} (${t.status}, to ${t.points_to_win || 21}, bo${t.best_of || 1})` }));
      populateSelect(document.getElementById('tournament_select'), items, 'tournament_id', 'label', null);
    }

    async function submitTournamentCreation(payload) {
      const resultEl = document.getElementById('create-tournament-result');
      resultEl.textContent = 'Creating...';
      try {
        const { res, data, error } = await authedFetch(`${API_BASE_URL}/create-tournament`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          resultEl.textContent = `Created tournament: ${data.name}`;
          document.getElementById('tournament_name').value = '';
          document.getElementById('filler-section').style.display = 'none';
          await loadTournamentsList();
          document.getElementById('tournament_select').value = data.tournament_id;
          renderTeamCompositionBars(data, 'team-composition-preview');
          renderTournament(data);
        } else {
          resultEl.textContent = `Error: ${error}`;
          nwAlert(`Tournament NOT created\n\n${error}`);
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    }

    let pendingTournamentPayload = null;

    document.getElementById('filler_new_toggle').addEventListener('change', (e) => {
      const useNew = e.target.checked;
      document.getElementById('filler-new-fields').style.display = useNew ? 'block' : 'none';
      document.getElementById('filler_existing_select').disabled = useNew;
    });

    document.getElementById('confirm-filler-btn').addEventListener('click', async () => {
      const fillerResultEl = document.getElementById('filler-result');
      const useNew = document.getElementById('filler_new_toggle').checked;
      let filler_player_id;

      if (useNew) {
        const newName = document.getElementById('filler_new_name').value.trim();
        const skill_level = document.getElementById('filler_new_skill').value;
        if (!newName) { fillerResultEl.textContent = 'Enter a name for the new player.'; return; }

        fillerResultEl.textContent = 'Registering...';
        try {
          const { res: regRes, data: regData } = await authedFetch(`${API_BASE_URL}/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName, skill_level })
          });
          if (!regRes.ok) { fillerResultEl.textContent = `Error registering: ${regData.error}`; return; }
          filler_player_id = regData.player_id;
          loadPlayers();
        } catch (err) {
          fillerResultEl.textContent = `Request failed: ${err.message}`;
          return;
        }
      } else {
        filler_player_id = document.getElementById('filler_existing_select').value;
        if (!filler_player_id) { fillerResultEl.textContent = 'Select an existing player, or register a new one.'; return; }
      }

      pendingTournamentPayload.filler_player_id = filler_player_id;
      await submitTournamentCreation(pendingTournamentPayload);
      pendingTournamentPayload = null;
    });

    document.getElementById('create-tournament-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const group_id = document.getElementById('tournament_group_select').value;
      const name = document.getElementById('tournament_name').value;
      const format = document.getElementById('tournament_format').value;
      const match_type = document.getElementById('tournament_match_type').value;
      const pairing_mode = document.getElementById('tournament_pairing_mode').value;
      const points_to_win = document.getElementById('tournament_points_to_win').value;
      const best_of = document.getElementById('tournament_best_of').value;
      const num_subgroups = document.getElementById('num_subgroups').value;
      const advance_per_group = document.getElementById('advance_per_group').value;
      const resultEl = document.getElementById('create-tournament-result');

      if (!group_id) { resultEl.textContent = 'Select a group first.'; return; }

      const payload = { group_id, name, format, match_type, pairing_mode, points_to_win, best_of, num_subgroups, advance_per_group };
      let participantCount = 0;
      if (pairing_mode === 'manual') {
        const manual_teams = collectManualTeams();
        if (manual_teams.length < 2) { resultEl.textContent = 'Add at least 2 complete teams.'; return; }
        payload.manual_teams = manual_teams;
      } else {
        const participant_ids = collectTournamentParticipants();
        if (participant_ids.length) {
          payload.participant_ids = participant_ids;
        }
        participantCount = participant_ids.length;
      }

      // Doubles needs an even headcount - if odd, pause and ask for a filler player
      if (pairing_mode !== 'manual' && match_type === 'doubles' && participantCount % 2 === 1) {
        const participant_ids = collectTournamentParticipants();
        const filterOut = new Set(participant_ids);
        const candidates = allPlayers.filter(p => !filterOut.has(p.player_id));
        const labeled = candidates.map(p => ({ ...p, label: `${p.name} (${p.rating})` }));
        populateSelect(document.getElementById('filler_existing_select'), labeled, 'player_id', 'label', null);
        document.getElementById('filler-result').textContent = '';
        document.getElementById('filler_new_toggle').checked = false;
        document.getElementById('filler-new-fields').style.display = 'none';
        document.getElementById('filler_existing_select').disabled = false;
        document.getElementById('filler-section').style.display = 'block';
        resultEl.textContent = `You have ${participantCount} players selected - doubles needs an even number.`;
        pendingTournamentPayload = payload;
        return;
      }

      document.getElementById('filler-section').style.display = 'none';
      await submitTournamentCreation(payload);
    });

    document.getElementById('load-tournament-btn').addEventListener('click', async () => {
      const tournamentId = document.getElementById('tournament_select').value;
      if (!tournamentId) return;
      const res = await fetch(`${API_BASE_URL}/tournaments/${tournamentId}`);
      const data = await res.json();
      renderTournament(data);
    });

    document.getElementById('delete-tournament-btn').addEventListener('click', async () => {
      const tournamentId = document.getElementById('tournament_select').value;
      const selectedOption = document.getElementById('tournament_select').selectedOptions[0];
      const resultEl = document.getElementById('delete-tournament-result');
      if (!tournamentId) { resultEl.textContent = 'Select a tournament first.'; return; }

      const label = selectedOption ? selectedOption.textContent : tournamentId;
      const confirmText = await nwPrompt(`Enter the confirmation code to delete "${label}". This only removes this exact tournament entry - player ratings and match history are untouched.`);
      if (!confirmText) {
        resultEl.textContent = 'Cancelled.';
        return;
      }

      resultEl.textContent = 'Deleting...';
      try {
        const res = await fetch(`${API_BASE_URL}/tournaments/${tournamentId}`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: confirmText })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = `Deleted: ${data.name} (also removed ${data.matches_deleted} related match record(s); ratings from those matches were not reverted)`;
          document.getElementById('tournament-detail').innerHTML = '';
          loadTournamentsList();
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    function collectAllEntities(t) {
      const entities = {};
      if (t.subgroups) {
        for (const sg of Object.values(t.subgroups)) {
          for (const e of sg.members) entities[e.player_id] = e;
        }
      }
      if (t.knockout && t.knockout.rounds && t.knockout.rounds[0]) {
        for (const m of t.knockout.rounds[0]) {
          if (m.player_a) entities[m.player_a.player_id] = m.player_a;
          if (m.player_b) entities[m.player_b.player_id] = m.player_b;
        }
      }
      return Object.values(entities).filter(e => e.members && e.members.length === 2);
    }

    function getAllTeamEntities(t) {
      const entities = {};
      if (t.subgroups) {
        for (const sg of Object.values(t.subgroups)) {
          for (const e of sg.members) entities[e.player_id] = e;
        }
      }
      if (t.knockout && t.knockout.rounds && t.knockout.rounds[0]) {
        for (const m of t.knockout.rounds[0]) {
          if (m.player_a) entities[m.player_a.player_id] = m.player_a;
          if (m.player_b) entities[m.player_b.player_id] = m.player_b;
        }
      }
      return Object.values(entities);
    }

    const TEAM_BAR_COLORS = ['#4a90d9', '#7ab8f5'];

    function renderTeamCompositionBars(t, containerId) {
      const container = document.getElementById(containerId);
      const entities = getAllTeamEntities(t);
      if (!entities.length) { container.innerHTML = ''; return; }

      const withRatings = entities.map(e => {
        const memberRatings = e.members.map((pid, idx) => {
          const p = allPlayers.find(pl => pl.player_id === pid);
          const hasSnapshot = e.member_ratings && e.member_ratings[idx] !== undefined;
          const rating = hasSnapshot ? Number(e.member_ratings[idx]) : (p ? Number(p.rating) : 1000);
          return { player_id: pid, name: p ? p.name : pid, rating };
        });
        const avg = memberRatings.reduce((sum, m) => sum + m.rating, 0) / memberRatings.length;
        return { name: e.name, members: memberRatings, avgRating: avg };
      });

      const maxAvg = Math.max(...withRatings.map(x => x.avgRating));
      withRatings.sort((a, b) => b.avgRating - a.avgRating);

      let html = '<h4 style="font-size:14px;">Team balance</h4><p style="font-size:12px;color:#555;">Ratings shown are as of when this tournament was created, not live.</p>';
      withRatings.forEach(team => {
        const widthPct = Math.max(15, (team.avgRating / maxAvg) * 100);
        html += `<div style="margin-bottom:10px;">
          <div style="font-size:13px; margin-bottom:2px;">${team.name} - avg Elo: ${Math.round(team.avgRating)}</div>
          <div style="display:flex; height:26px; border:1px solid #ccc; width:${widthPct}%; min-width:180px;">`;
        team.members.forEach((m, idx) => {
          const flexBasis = m.rating;
          const color = TEAM_BAR_COLORS[idx % TEAM_BAR_COLORS.length];
          html += `<div style="flex:${flexBasis}; background:${color}; color:white; font-size:11px; display:flex; align-items:center; justify-content:center; overflow:hidden; white-space:nowrap;">${m.name} (${m.rating})</div>`;
        });
        html += `</div></div>`;
      });
      container.innerHTML = html;
    }

    function populateSubstitutionSection(t) {
      const section = document.getElementById('substitution-section');
      const entities = collectAllEntities(t);
      if (!entities.length) { section.style.display = 'none'; return; }
      section.style.display = 'block';

      const teamOptions = entities.map(e => ({ player_id: e.player_id, label: e.name }));
      populateSelect(document.getElementById('sub_team_select'), teamOptions, 'player_id', 'label', null);
      updateSubOldPlayerOptions();
    }

    function updateSubOldPlayerOptions() {
      const teamId = document.getElementById('sub_team_select').value;
      const entities = collectAllEntities(currentTournamentData);
      const team = entities.find(e => e.player_id === teamId);
      if (!team) return;

      const memberOptions = team.members.map(pid => {
        const p = allPlayers.find(pl => pl.player_id === pid);
        return { player_id: pid, label: p ? `${p.name} (${p.nickname})` : pid };
      });
      populateSelect(document.getElementById('sub_old_player_select'), memberOptions, 'player_id', 'label', null);

      const labeled = allPlayers.map(p => ({ ...p, label: `${p.name} (${p.nickname}) (${p.rating})` }));
      populateSelect(document.getElementById('sub_new_player_select'), labeled, 'player_id', 'label', null);
    }

    document.getElementById('sub_team_select').addEventListener('change', updateSubOldPlayerOptions);

    document.getElementById('sub_new_player_toggle').addEventListener('change', (e) => {
      const useNew = e.target.checked;
      document.getElementById('sub_new_player_register_fields').style.display = useNew ? 'block' : 'none';
      document.getElementById('sub_new_player_select').disabled = useNew;
    });

    document.getElementById('submit-substitution-btn').addEventListener('click', async () => {
      const team_entity_id = document.getElementById('sub_team_select').value;
      const old_player_id = document.getElementById('sub_old_player_select').value;
      const resultEl = document.getElementById('substitution-result');
      const isNewPlayer = document.getElementById('sub_new_player_toggle').checked;

      if (!team_entity_id || !old_player_id) {
        resultEl.textContent = 'Select a team and the player being replaced.';
        return;
      }

      let new_player_id;

      if (isNewPlayer) {
        const newName = document.getElementById('sub_new_player_name').value.trim();
        const skill_level = document.getElementById('sub_new_player_skill').value;
        if (!newName) { resultEl.textContent = 'Enter a name for the new player.'; return; }

        resultEl.textContent = 'Registering new player...';
        try {
          const { res: regRes, data: regData } = await authedFetch(`${API_BASE_URL}/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName, skill_level })
          });
          if (!regRes.ok) { resultEl.textContent = `Error registering: ${regData.error}`; return; }
          new_player_id = regData.player_id;
          loadPlayers();
        } catch (err) {
          resultEl.textContent = `Request failed while registering: ${err.message}`;
          return;
        }
      } else {
        new_player_id = document.getElementById('sub_new_player_select').value;
        if (!new_player_id) { resultEl.textContent = 'Select a replacement player.'; return; }
      }

      if (old_player_id === new_player_id) {
        resultEl.textContent = 'Replacement must be a different player.';
        return;
      }

      resultEl.textContent = 'Substituting...';
      try {
        const res = await fetch(`${API_BASE_URL}/tournaments/${currentTournamentData.tournament_id}/substitute`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ team_entity_id, old_player_id, new_player_id })
        });
        const data = await res.json();
        if (res.ok) {
          resultEl.textContent = 'Substitution applied to future matches.';
          document.getElementById('sub_new_player_toggle').checked = false;
          document.getElementById('sub_new_player_register_fields').style.display = 'none';
          document.getElementById('sub_new_player_select').disabled = false;
          document.getElementById('sub_new_player_name').value = '';
          renderTournament(data);
        } else {
          resultEl.textContent = `Error: ${data.error}`;
        }
      } catch (err) {
        resultEl.textContent = `Request failed: ${err.message}`;
      }
    });

    function formatGames(games) {
      if (!games || !games.length) return '';
      return games.map(g => `${g.score_a}-${g.score_b}`).join(', ');
    }

    document.getElementById('tournament_view_mode').addEventListener('change', applyTournamentViewMode);

    function applyTournamentViewMode() {
      const mode = document.getElementById('tournament_view_mode').value;
      document.getElementById('tournament-detail').style.display = mode === 'table' ? 'block' : 'none';
      document.getElementById('bracket-svg').style.display = mode === 'bracket' ? 'block' : 'none';
    }

    function matchTotals(match) {
      const games = match.games || [];
      return {
        a: games.reduce((sum, g) => sum + Number(g.score_a), 0),
        b: games.reduce((sum, g) => sum + Number(g.score_b), 0)
      };
    }

    function truncateBracketName(name, maxChars = 22) {
      if (!name) return name;
      return name.length > maxChars ? name.slice(0, maxChars - 1) + '…' : name;
    }

    function renderBracketView(t) {
      const svg = document.getElementById('bracket-svg');
      if (!t.knockout || !t.knockout.rounds || !t.knockout.rounds.length) {
        svg.innerHTML = '<text x="10" y="20" font-size="13" fill="var(--text-secondary)">No knockout bracket for this tournament yet.</text>';
        svg.setAttribute('viewBox', '0 0 400 40');
        return;
      }

      const dataRounds = t.knockout.rounds;
      const colWidth = 240;
      const boxWidth = 200;
      const boxHeight = 56;
      const baseSpacing = 90;

      // Round 1's match count is fixed from the moment the bracket is
      // created and always a power of 2 - so the TRUE total number of
      // rounds (through the real Final) is computable up front, even
      // before later rounds exist in the data. Using dataRounds.length
      // instead would mislabel "Round 1" as "Final" whenever it's the
      // only round computed so far.
      const totalRounds = Math.round(Math.log2(dataRounds[0].length)) + 1;

      // Build a full virtual bracket through totalRounds, filling in any
      // not-yet-computed future rounds with TBD placeholder matches, so
      // the complete bracket shape is visible from round 1 onward.
      const rounds = [];
      for (let r = 0; r < totalRounds; r++) {
        if (dataRounds[r]) {
          rounds[r] = dataRounds[r];
        } else {
          const prevCount = rounds[r - 1] ? rounds[r - 1].length : dataRounds[0].length;
          const count = Math.max(1, Math.ceil(prevCount / 2));
          rounds[r] = Array.from({ length: count }, () => ({
            player_a: null, player_b: null, games: [], played: false, winner_id: null
          }));
        }
      }

      const positions = [];
      positions[0] = rounds[0].map((_, i) => i * baseSpacing + baseSpacing / 2);
      for (let r = 1; r < rounds.length; r++) {
        positions[r] = rounds[r].map((_, i) => {
          const y1 = positions[r - 1][i * 2];
          const y2 = positions[r - 1][i * 2 + 1];
          if (y1 !== undefined && y2 !== undefined) return (y1 + y2) / 2;
          return y1 !== undefined ? y1 : baseSpacing / 2;
        });
      }

      const finalBoxTop = positions[rounds.length - 1][0] - boxHeight / 2 + 24;
      const finalBoxBottom = finalBoxTop + boxHeight;
      const hasThirdPlace = item_has_third_place(t) && t.knockout.third_place_match;
      const thirdPlaceGap = 40;
      const totalHeight = Math.max(
        Math.max(...positions[0]) + baseSpacing / 2 + 30,
        hasThirdPlace ? finalBoxBottom + thirdPlaceGap + boxHeight + 20 : 0
      );
      const totalWidth = rounds.length * colWidth + boxWidth + 40;

      let content = '';

      for (let r = 1; r < rounds.length; r++) {
        for (let i = 0; i < rounds[r].length; i++) {
          const x1 = 20 + (r - 1) * colWidth + boxWidth;
          const x2 = 20 + r * colWidth;
          const xMid = x1 + (x2 - x1) / 2;
          const yThis = positions[r][i];
          const yFeed1 = positions[r - 1][i * 2];
          const yFeed2 = positions[r - 1][i * 2 + 1];
          if (yFeed1 !== undefined) content += `<path d="M${x1},${yFeed1} H${xMid} V${yThis} H${x2}" fill="none" stroke="var(--border)" stroke-width="2"/>`;
          if (yFeed2 !== undefined) content += `<path d="M${x1},${yFeed2} H${xMid} V${yThis} H${x2}" fill="none" stroke="var(--border)" stroke-width="2"/>`;
        }
      }

      rounds.forEach((round, r) => {
        const x = 20 + r * colWidth;
        const label = r === totalRounds - 1 ? 'Final' : (r === totalRounds - 2 ? 'Semifinal' : `Round ${r + 1}`);
        content += `<text x="${x}" y="16" font-size="12" fill="var(--text-secondary)">${label}</text>`;
        round.forEach((match, i) => {
          const y = positions[r][i] - boxHeight / 2 + 24;
          const nameA = truncateBracketName(match.player_a ? match.player_a.name : 'TBD');
          const nameB = match.bye ? 'BYE' : truncateBracketName(match.player_b ? match.player_b.name : 'TBD');
          const totals = matchTotals(match);
          const aWon = match.winner_id && match.player_a && match.winner_id === match.player_a.player_id;
          const bWon = match.winner_id && match.player_b && match.winner_id === match.player_b.player_id;
          const isPlaceholder = !match.player_a && !match.player_b;

          content += `<rect x="${x}" y="${y}" width="${boxWidth}" height="${boxHeight}" fill="var(--surface)" stroke="var(--border)" rx="6" ${isPlaceholder ? 'stroke-dasharray="4 3"' : ''}/>`;
          content += `<line x1="${x}" y1="${y + boxHeight / 2}" x2="${x + boxWidth}" y2="${y + boxHeight / 2}" stroke="var(--border)"/>`;
          content += `<text x="${x + 8}" y="${y + 20}" font-size="13" font-weight="${aWon ? 'bold' : 'normal'}" fill="${aWon ? 'var(--court)' : 'var(--text-secondary)'}">${nameA}</text>`;
          content += `<text x="${x + boxWidth - 8}" y="${y + 20}" font-size="13" text-anchor="end" fill="var(--text-secondary)">${match.played && !match.bye ? totals.a : ''}</text>`;
          content += `<text x="${x + 8}" y="${y + boxHeight - 8}" font-size="13" font-weight="${bWon ? 'bold' : 'normal'}" fill="${bWon ? 'var(--court)' : 'var(--text-secondary)'}">${nameB}</text>`;
          content += `<text x="${x + boxWidth - 8}" y="${y + boxHeight - 8}" font-size="13" text-anchor="end" fill="var(--text-secondary)">${match.played && !match.bye ? totals.b : ''}</text>`;
        });
      });

      if (hasThirdPlace) {
        const tp = t.knockout.third_place_match;
        const x = 20 + (rounds.length - 1) * colWidth;
        const y = finalBoxBottom + thirdPlaceGap;
        const totals = matchTotals(tp);
        const aWon = tp.winner_id && tp.player_a && tp.winner_id === tp.player_a.player_id;
        const bWon = tp.winner_id && tp.player_b && tp.winner_id === tp.player_b.player_id;
        content += `<text x="${x}" y="${y - 8}" font-size="12" fill="var(--text-secondary)">3rd place</text>`;
        content += `<rect x="${x}" y="${y}" width="${boxWidth}" height="${boxHeight}" fill="var(--surface)" stroke="var(--border)" rx="6"/>`;
        content += `<line x1="${x}" y1="${y + boxHeight / 2}" x2="${x + boxWidth}" y2="${y + boxHeight / 2}" stroke="var(--border)"/>`;
        content += `<text x="${x + 8}" y="${y + 20}" font-size="13" font-weight="${aWon ? 'bold' : 'normal'}" fill="${aWon ? 'var(--court)' : 'var(--text)'}">${truncateBracketName(tp.player_a ? tp.player_a.name : 'TBD')}</text>`;
        content += `<text x="${x + boxWidth - 8}" y="${y + 20}" font-size="13" text-anchor="end" fill="var(--text-secondary)">${tp.played ? totals.a : ''}</text>`;
        content += `<text x="${x + 8}" y="${y + boxHeight - 8}" font-size="13" font-weight="${bWon ? 'bold' : 'normal'}" fill="${bWon ? 'var(--court)' : 'var(--text)'}">${truncateBracketName(tp.player_b ? tp.player_b.name : 'TBD')}</text>`;
        content += `<text x="${x + boxWidth - 8}" y="${y + boxHeight - 8}" font-size="13" text-anchor="end" fill="var(--text-secondary)">${tp.played ? totals.b : ''}</text>`;
      }

      svg.setAttribute('viewBox', `0 0 ${totalWidth} ${totalHeight}`);
      svg.innerHTML = content;
    }

    function renderTournament(t) {
      currentTournamentData = t;
      populateSubstitutionSection(t);
      renderTeamCompositionBars(t, 'team-composition-preview');
      renderBracketView(t);
      applyTournamentViewMode();
      const el = document.getElementById('tournament-detail');
      const bestOf = t.best_of || 1;
      const liveMode = document.getElementById('tournament_live_toggle').checked;
      // Guests can watch a tournament but not score it - the same rule the
      // Matches tab already applies to recording a match.
      const canScore = isLoggedIn() && hasLinkedPlayer();
      const target = parseInt(t.points_to_win, 10) || 21;
      let html = `<h3 style="font-size:16px;">${t.name} - ${t.status} (games to ${t.points_to_win || 21}, best of ${bestOf})</h3>`;

      if (t.subgroups) {
        for (const [sgName, sg] of Object.entries(t.subgroups)) {
          html += `<h4 style="font-size:14px;">Group ${sgName}</h4>`;
          const standings = (t.standings || {})[sgName] || [];
          if (standings.length) {
            html += '<table><tr><th>Player</th><th>W</th><th>L</th><th>Diff</th></tr>';
            standings.forEach(s => {
              html += `<tr><td>${s.name}</td><td>${s.wins}</td><td>${s.losses}</td><td>${s.point_diff}</td></tr>`;
            });
            html += '</table>';
          }
          sg.fixtures.forEach(f => {
            const tbPrefix = f.tiebreaker ? '<strong>[TIEBREAKER]</strong> ' : '';
            const label = `${tbPrefix}${f.player_a.name} vs ${f.player_b.name}`;
            const gamesText = formatGames(f.games);
            const lastGame = (f.games && f.games.length) ? f.games[f.games.length - 1] : null;
            const vsCard = renderVsCard(vsSideIds(f.player_a), vsSideIds(f.player_b), {
              snapshot: t.card_snapshot,
              isFinal: false,
              scoreA: gameScore(lastGame, 'a'),
              scoreB: gameScore(lastGame, 'b'),
              winner: f.played ? (f.games_won_a > f.games_won_b ? 'a' : 'b') : null
            });
            const head = vsCard ? `${tbPrefix}${vsCard}` : `<p>${label}</p>`;
            html += `<div class="fixture">${head}`;
            if (f.played) {
              // Score and winner are drawn on the card itself, so there's
              // nothing to repeat underneath it. Multi-game matches keep a
              // small per-game breakdown, since the card shows only the
              // decisive one.
              if (f.games && f.games.length > 1) {
                html += `<div class="fixture-controls" style="opacity:.75;">${gamesText}</div>`;
              }
            } else if (!canScore) {
              // Same rule as the Matches tab: recording a result is a
              // members-only action, so guests see the fixture but no inputs.
              html += `<div class="fixture-controls">Log in to enter a score.</div>`;
            } else {
              const nextGameNum = (f.games ? f.games.length : 0) + 1;
              const progress = gamesText ? `Games so far: ${gamesText} (${f.games_won_a}-${f.games_won_b}) | ` : '';
              const matchKey = `group_${f.fixture_id}`;
              if (liveMode) {
                html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                  ${renderLiveScoreControls(matchKey, target, `finishGroupLiveGame('${matchKey}','${t.tournament_id}','${sgName}','${f.fixture_id}')`, f.player_a.name, f.player_b.name)}</div>`;
              } else {
                html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                  <input type="number" id="ga_${f.fixture_id}">
                  -
                  <input type="number" id="gb_${f.fixture_id}">
                  <button onclick="submitGroupScore('${t.tournament_id}','${sgName}','${f.fixture_id}')">Submit game ${nextGameNum}</button></div>`;
              }
            }
            html += '</div>';
          });
        }
      }

      if (t.status === 'knockout' || t.status === 'completed') {
        t.knockout.rounds.forEach((round, rIdx) => {
          const isFinalRound = rIdx === t.knockout.rounds.length - 1;
          html += `<h4 style="font-size:14px;">${isFinalRound ? 'Final' : 'Round ' + (rIdx + 1)}</h4>`;
          // A round is the array of matches itself in this payload, not an
          // object wrapping one. Accept either shape so this can't break
          // again on a format that nests them.
          const roundMatches = Array.isArray(round) ? round : (round.matches || []);
          roundMatches.forEach((m, mIdx) => {
            const bName = m.player_b ? m.player_b.name : 'BYE';
            const label = `${m.player_a.name} vs ${bName}`;
            const gamesText = formatGames(m.games);
            const lastGame = (m.games && m.games.length) ? m.games[m.games.length - 1] : null;
            const card = m.player_b ? renderVsCard(vsSideIds(m.player_a), vsSideIds(m.player_b), {
              snapshot: t.card_snapshot,
              // Only the last round's match is the actual final - that's
              // what earns the cup rather than the VS badge.
              isFinal: isFinalRound,
              scoreA: gameScore(lastGame, 'a'),
              scoreB: gameScore(lastGame, 'b'),
              winner: m.played ? (m.games_won_a > m.games_won_b ? 'a' : 'b') : null
            }) : '';
            html += `<div class="fixture">${card || `<p>${label}</p>`}`;
            if (m.played) {
              if (m.games && m.games.length > 1) {
                html += `<div class="fixture-controls" style="opacity:.75;">${gamesText}</div>`;
              }
            } else if (!m.player_b) {
              html += `<div class="fixture-controls">${label} &middot; walkover</div>`;
            } else if (!canScore) {
              html += `<div class="fixture-controls">Log in to enter a score.</div>`;
            } else {
              const nextGameNum = (m.games ? m.games.length : 0) + 1;
              const progress = gamesText ? `Games so far: ${gamesText} (${m.games_won_a}-${m.games_won_b}) | ` : '';
              const matchKey = `ko_${t.tournament_id}_${rIdx}_${mIdx}`;
              if (liveMode) {
                html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                  ${renderLiveScoreControls(matchKey, target, `finishKnockoutLiveGame('${matchKey}','${t.tournament_id}',${rIdx},${mIdx})`, m.player_a.name, bName)}</div>`;
              } else {
                html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                  <input type="number" id="ka_${t.tournament_id}_${rIdx}_${mIdx}">
                  -
                  <input type="number" id="kb_${t.tournament_id}_${rIdx}_${mIdx}">
                  <button onclick="submitKnockoutScore('${t.tournament_id}',${rIdx},${mIdx})">Submit game ${nextGameNum}</button></div>`;
              }
            }
            html += '</div>';
          });
        });

        if (item_has_third_place(t)) {
          const tp = t.knockout.third_place_match;
          const label = `${tp.player_a.name} vs ${tp.player_b.name}`;
          const gamesText = formatGames(tp.games);
          const tpLast = (tp.games && tp.games.length) ? tp.games[tp.games.length - 1] : null;
          const tpCard = renderVsCard(vsSideIds(tp.player_a), vsSideIds(tp.player_b), {
            snapshot: t.card_snapshot,
            // Third place gets the VS badge, not the cup - the cup means
            // the title, and putting one here would flatten the difference.
            isFinal: false,
            scoreA: gameScore(tpLast, 'a'),
            scoreB: gameScore(tpLast, 'b'),
            winner: tp.played ? (tp.games_won_a > tp.games_won_b ? 'a' : 'b') : null
          });
          html += `<h4 style="font-size:14px;">3rd Place Match</h4>`;
          html += `<div class="fixture">${tpCard || `<p>${label}</p>`}`;
          if (tp.played) {
            if (tp.games && tp.games.length > 1) {
              html += `<div class="fixture-controls" style="opacity:.75;">${gamesText}</div>`;
            }
          } else if (!canScore) {
            html += `<div class="fixture-controls">Log in to enter a score.</div>`;
          } else {
            const nextGameNum = (tp.games ? tp.games.length : 0) + 1;
            const progress = gamesText ? `Games so far: ${gamesText} (${tp.games_won_a}-${tp.games_won_b}) | ` : '';
            const matchKey = `thirdplace_${t.tournament_id}`;
            if (liveMode) {
              html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                ${renderLiveScoreControls(matchKey, target, `finishThirdPlaceLiveGame('${matchKey}','${t.tournament_id}')`, tp.player_a.name, tp.player_b.name)}</div>`;
            } else {
              html += `<div class="fixture-controls">${progress}Game ${nextGameNum}:
                <input type="number" id="tpa_${t.tournament_id}">
                -
                <input type="number" id="tpb_${t.tournament_id}">
                <button onclick="submitThirdPlaceScore('${t.tournament_id}')">Submit game ${nextGameNum}</button></div>`;
            }
          }
          html += '</div>';
        }

        if (t.status === 'completed') {
          const finalMatch = t.knockout.rounds[t.knockout.rounds.length - 1][0];
          const championName = finalMatch.player_a.player_id === finalMatch.winner_id ? finalMatch.player_a.name : finalMatch.player_b.name;
          html += `<p><strong>Champion: ${championName}</strong></p>`;
          html += `<button type="button" class="secondary" onclick="copyTournamentRecap()">Copy WhatsApp recap</button>`;
          html += `<button type="button" onclick="downloadTournamentImage()" style="margin-left:8px;">Download share image</button>`;
        }
      }

      el.innerHTML = html;
    }

    function generateTournamentRecap(t) {
      if (!t.knockout || t.status !== 'completed') return null;
      const finalMatch = t.knockout.rounds[t.knockout.rounds.length - 1][0];
      const isChampionA = finalMatch.player_a.player_id === finalMatch.winner_id;
      const championName = isChampionA ? finalMatch.player_a.name : finalMatch.player_b.name;
      const runnerUpName = isChampionA ? finalMatch.player_b.name : finalMatch.player_a.name;

      let recap = `🏆 ${t.name} - Results\n\n`;
      recap += `Champion: ${championName}\n`;
      recap += `Runner-up: ${runnerUpName}\n`;
      recap += `Final score: ${formatGames(finalMatch.games)}\n`;

      if (item_has_third_place(t) && t.knockout.third_place_match.winner_id) {
        const tp = t.knockout.third_place_match;
        const tpWinnerName = tp.player_a.player_id === tp.winner_id ? tp.player_a.name : tp.player_b.name;
        recap += `3rd place: ${tpWinnerName}\n`;
      }
      return recap;
    }

    /**
     * Renders the tournament's final (and semifinals if present) to a PNG
     * the user can save and share on WhatsApp. Done on a canvas rather than
     * server-side: WhatsApp can't render our CSS cards, but it happily
     * attaches a downloaded image, and this needs no new Lambda or S3
     * round trip. Player photos are drawn when present, falling back to the
     * preset colour so a card always renders.
     *
     * crossOrigin='anonymous' on the images matters: our uploads serve from
     * CloudFront with permissive CORS, and without it the canvas would be
     * tainted and toBlob() would throw a security error.
     */
    async function downloadTournamentImage() {
      const t = currentTournamentData;
      if (!t || !t.knockout || t.status !== 'completed') {
        nwAlert('This tournament is not completed yet.');
        return;
      }
      const rounds = t.knockout.rounds;
      const final = rounds[rounds.length - 1][0];
      const semis = rounds.length >= 2 ? rounds[rounds.length - 2] : [];

      const W = 900, cardH = 200, pad = 40, gap = 24;
      const rows = 1 + (semis.length ? 1 : 0) + semis.length;
      const H = pad * 2 + 70 + cardH + (semis.length ? (40 + semis.length * (cardH + gap)) : 0);
      const canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H;
      const ctx = canvas.getContext('2d');

      ctx.fillStyle = '#0F1B15';
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = '#fff';
      ctx.font = "800 34px 'Rajdhani', system-ui, sans-serif";
      ctx.textBaseline = 'top';
      ctx.fillText(t.name, pad, pad);
      ctx.font = "600 16px system-ui, sans-serif";
      ctx.fillStyle = '#9fb3a8';
      ctx.fillText('Tournament results', pad, pad + 40);

      const loadImg = (src) => new Promise((res) => {
        if (!src) { res(null); return; }
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = () => res(img);
        img.onerror = () => res(null);
        img.src = src;
      });

      // Pre-resolve every avatar image up front so drawing is synchronous.
      const sideVisuals = (side) => vsSideIds(side).map(id => vsPlayerVisual(id, t.card_snapshot));
      const allSides = [ [final.player_a, final.player_b], ...semis.map(m => [m.player_a, m.player_b]) ];
      const imgCache = {};
      await Promise.all(allSides.flat().flatMap(sideVisuals).map(async v => {
        if (v.avatarUrl && !(v.avatarUrl in imgCache)) imgCache[v.avatarUrl] = await loadImg(v.avatarUrl);
      }));

      async function drawCard(x, y, w, match, isFinal) {
        const a = sideVisuals(match.player_a), b = sideVisuals(match.player_b);
        const winA = match.winner_id && match.player_a.player_id === match.winner_id;
        const winB = match.winner_id && match.player_b.player_id === match.winner_id;

        ctx.save();
        roundRect(ctx, x, y, w, cardH, 14); ctx.clip();
        // team B fills, team A masked over — approximated here with a hard
        // split, since canvas gradient masks are heavier than they're worth
        // for a static export.
        paintTeam(ctx, x, y, w, cardH, b, '#3a1114');
        ctx.save();
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + w * 0.52, y);
        ctx.lineTo(x + w * 0.42, y + cardH); ctx.lineTo(x, y + cardH); ctx.closePath(); ctx.clip();
        paintTeam(ctx, x, y, w, cardH, a, '#06231c');
        ctx.restore();
        ctx.restore();

        drawAvatars(ctx, x + 26, y + 30, a, winA);
        drawAvatars(ctx, x + w - 26 - a.length * 62, y + cardH - 92, b, winB);

        // centre badge or cup
        ctx.textAlign = 'center';
        if (isFinal) {
          ctx.font = "800 30px system-ui"; ctx.fillStyle = '#F5C542';
          ctx.fillText('🏆', x + w / 2, y + cardH / 2 - 24);
          ctx.font = "800 13px 'Rajdhani', sans-serif";
          ctx.fillText('FINAL', x + w / 2, y + cardH / 2 + 16);
        } else {
          ctx.font = "800 italic 34px 'Rajdhani', sans-serif"; ctx.fillStyle = '#fff';
          ctx.shadowColor = '#00b4d8'; ctx.shadowBlur = 18;
          ctx.fillText('VS', x + w / 2, y + cardH / 2 - 18);
          ctx.shadowBlur = 0;
        }
        // score
        const g = match.games && match.games.length ? match.games[match.games.length - 1] : null;
        if (g) {
          ctx.font = "800 26px 'Rajdhani', sans-serif";
          ctx.fillStyle = winA ? '#F5C542' : '#fff'; ctx.textAlign = 'left';
          ctx.fillText(String(g.score_a), x + 26, y + cardH - 40);
          ctx.fillStyle = winB ? '#F5C542' : '#fff'; ctx.textAlign = 'right';
          ctx.fillText(String(g.score_b), x + w - 26, y + 22);
        }
        ctx.textAlign = 'left';
      }

      function drawAvatars(ctx, x, y, side, isWinner) {
        side.forEach((v, i) => {
          const cx = x + i * 62 + 26, cy = y + 26, r = 24;
          ctx.save();
          ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
          ctx.lineWidth = 3; ctx.strokeStyle = isWinner ? '#F5C542' : 'rgba(255,255,255,.92)'; ctx.stroke();
          ctx.clip();
          const img = imgCache[v.avatarUrl];
          if (img) { ctx.drawImage(img, cx - r, cy - r, r * 2, r * 2); }
          else { ctx.fillStyle = '#1c2a22'; ctx.fillRect(cx - r, cy - r, r * 2, r * 2);
                 ctx.fillStyle = '#fff'; ctx.font = '22px system-ui'; ctx.textAlign = 'center';
                 ctx.fillText(v.avatarEmoji || '', cx, cy - 12); }
          ctx.restore();
          ctx.fillStyle = '#fff'; ctx.font = "600 12px system-ui"; ctx.textAlign = 'center';
          ctx.fillText((v.name || '').slice(0, 10), cx, cy + r + 6);
          ctx.textAlign = 'left';
        });
      }

      function paintTeam(ctx, x, y, w, h, side, fallback) {
        const g = ctx.createLinearGradient(x, y, x + w, y + h);
        g.addColorStop(0, fallback); g.addColorStop(1, '#12251d');
        ctx.fillStyle = g; ctx.fillRect(x, y, w, h);
      }

      let cursorY = pad + 70;
      await drawCard(pad, cursorY, W - pad * 2, final, true);
      cursorY += cardH + 40;
      if (semis.length) {
        ctx.fillStyle = '#9fb3a8'; ctx.font = "700 18px 'Rajdhani', sans-serif";
        ctx.fillText('Semifinals', pad, cursorY - 30);
        for (const sm of semis) {
          await drawCard(pad, cursorY, W - pad * 2, sm, false);
          cursorY += cardH + gap;
        }
      }

      canvas.toBlob((blob) => {
        if (!blob) { nwAlert('Could not generate the image.'); return; }
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = `${t.name.replace(/[^a-z0-9]+/gi, '_')}_results.png`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }, 'image/png');
    }

    function roundRect(ctx, x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
    }

    function copyTournamentRecap() {
      if (!currentTournamentData) return;
      const recap = generateTournamentRecap(currentTournamentData);
      if (!recap) { nwAlert('This tournament is not completed yet.'); return; }
      navigator.clipboard.writeText(recap).then(() => {
        nwAlert('Recap copied - paste it into WhatsApp.');
      }).catch(async () => {
        await nwPrompt('Copy this text manually:', recap);
      });
    }

    function item_has_third_place(t) {
      return t.knockout && t.knockout.third_place_match;
    }

    async function submitGroupScore(tournamentId, subgroup, fixtureId) {
      const score_a = document.getElementById(`ga_${fixtureId}`).value;
      const score_b = document.getElementById(`gb_${fixtureId}`).value;
      await submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score_a, score_b);
    }

    async function submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score_a, score_b, override, pointLog) {
      const res = await fetch(`${API_BASE_URL}/tournaments/${tournamentId}/group-score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subgroup, fixture_id: fixtureId, score_a, score_b, override: !!override, point_log: pointLog || undefined })
      });
      const data = await res.json();
      if (res.ok) {
        renderTournament(data);
        loadPlayers();
      } else if (!override && data.error && data.error.startsWith('invalid game score')) {
        if (await nwConfirm(`${data.error}\n\nThis doesn't match the tournament's configured scoring rules. Submit ${score_a}-${score_b} anyway as the actual result?`)) {
          await submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score_a, score_b, true, pointLog);
        }
      } else {
        nwAlert(data.error);
      }
    }

    async function submitKnockoutScore(tournamentId, roundIndex, matchIndex) {
      const score_a = document.getElementById(`ka_${tournamentId}_${roundIndex}_${matchIndex}`).value;
      const score_b = document.getElementById(`kb_${tournamentId}_${roundIndex}_${matchIndex}`).value;
      await submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, score_a, score_b);
    }

    async function submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, score_a, score_b, override, pointLog) {
      const res = await fetch(`${API_BASE_URL}/tournaments/${tournamentId}/knockout-score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ round_index: roundIndex, match_index: matchIndex, score_a, score_b, override: !!override, point_log: pointLog || undefined })
      });
      const data = await res.json();
      if (res.ok) {
        renderTournament(data);
        loadPlayers();
      } else if (!override && data.error && data.error.startsWith('invalid game score')) {
        if (await nwConfirm(`${data.error}\n\nThis doesn't match the tournament's configured scoring rules. Submit ${score_a}-${score_b} anyway as the actual result?`)) {
          await submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, score_a, score_b, true, pointLog);
        }
      } else {
        nwAlert(data.error);
      }
    }

    async function submitThirdPlaceScore(tournamentId) {
      const score_a = document.getElementById(`tpa_${tournamentId}`).value;
      const score_b = document.getElementById(`tpb_${tournamentId}`).value;
      await submitThirdPlaceScoreDirect(tournamentId, score_a, score_b);
    }

    async function submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override, pointLog) {
      const res = await fetch(`${API_BASE_URL}/tournaments/${tournamentId}/knockout-score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ third_place: true, score_a, score_b, override: !!override, point_log: pointLog || undefined })
      });
      const data = await res.json();
      if (res.ok) {
        renderTournament(data);
        loadPlayers();
      } else if (!override && data.error && data.error.startsWith('invalid game score')) {
        if (await nwConfirm(`${data.error}\n\nThis doesn't match the tournament's configured scoring rules. Submit ${score_a}-${score_b} anyway as the actual result?`)) {
          await submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, true, pointLog);
        }
      } else {
        nwAlert(data.error);
      }
    }

    // ---------- live scoring inside tournament matches ----------

    const tournamentLiveLogs = {};

    function getTournamentLiveLog(matchKey) {
      if (!tournamentLiveLogs[matchKey]) tournamentLiveLogs[matchKey] = [];
      return tournamentLiveLogs[matchKey];
    }

    function tournamentLivePoint(matchKey, side, target) {
      const log = getTournamentLiveLog(matchKey);
      const a = log.filter(p => p === 'A').length;
      const b = log.filter(p => p === 'B').length;
      if (isGameOver(a, b, target)) return;
      log.push(side);
      updateTournamentLiveDisplay(matchKey, target);
    }

    function tournamentUndoPoint(matchKey, target) {
      const log = getTournamentLiveLog(matchKey);
      log.pop();
      updateTournamentLiveDisplay(matchKey, target);
    }

    function updateTournamentLiveDisplay(matchKey, target) {
      const log = getTournamentLiveLog(matchKey);
      const a = log.filter(p => p === 'A').length;
      const b = log.filter(p => p === 'B').length;
      const over = isGameOver(a, b, target);
      const displayEl = document.getElementById(`tlive_display_${matchKey}`);
      if (displayEl) {
        let text = `${a} - ${b}`;
        if (over) text += a > b ? ' - A wins this game' : ' - B wins this game';
        displayEl.textContent = text;
      }
      const btnA = document.getElementById(`tlive_btn_a_${matchKey}`);
      const btnB = document.getElementById(`tlive_btn_b_${matchKey}`);
      if (btnA) btnA.disabled = over;
      if (btnB) btnB.disabled = over;
      if (splitScreenMatchKey === matchKey) updateSplitScreenScores(a, b, over);
    }

    async function finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtureId) {
      const log = getTournamentLiveLog(matchKey);
      const a = log.filter(p => p === 'A').length;
      const b = log.filter(p => p === 'B').length;
      if (a === b) { nwAlert('Record at least one decisive point before submitting.'); return; }
      delete tournamentLiveLogs[matchKey];
      await submitGroupScoreDirect(tournamentId, subgroup, fixtureId, a, b, false, log);
    }

    async function finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matchIndex) {
      const log = getTournamentLiveLog(matchKey);
      const a = log.filter(p => p === 'A').length;
      const b = log.filter(p => p === 'B').length;
      if (a === b) { nwAlert('Record at least one decisive point before submitting.'); return; }
      delete tournamentLiveLogs[matchKey];
      await submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, a, b, false, log);
    }

    async function finishThirdPlaceLiveGame(matchKey, tournamentId) {
      const log = getTournamentLiveLog(matchKey);
      const a = log.filter(p => p === 'A').length;
      const b = log.filter(p => p === 'B').length;
      if (a === b) { nwAlert('Record at least one decisive point before submitting.'); return; }
      delete tournamentLiveLogs[matchKey];
      await submitThirdPlaceScoreDirect(tournamentId, a, b, false, log);
    }

    function renderLiveScoreControls(matchKey, target, finishCallExpr, nameA, nameB) {
      const safeNameA = (nameA || 'Team A').replace(/'/g, "\\'");
      const safeNameB = (nameB || 'Team B').replace(/'/g, "\\'");
      return `
        <div>
          <button id="tlive_btn_a_${matchKey}" type="button" onclick="tournamentLivePoint('${matchKey}','A',${target})">+1 A</button>
          <span id="tlive_display_${matchKey}" style="margin: 0 10px; font-weight:bold;">0 - 0</span>
          <button id="tlive_btn_b_${matchKey}" type="button" onclick="tournamentLivePoint('${matchKey}','B',${target})">+1 B</button>
          <button type="button" onclick="tournamentUndoPoint('${matchKey}',${target})">Undo</button>
          <button type="button" onclick="${finishCallExpr}">Submit game</button>
          <button type="button" class="secondary" onclick="openTournamentSplitScreen('${matchKey}', ${target}, '${safeNameA}', '${safeNameB}', () => { ${finishCallExpr} })">Split-screen</button>
        </div>`;
    }

    document.getElementById('tournament_live_toggle').addEventListener('change', () => {
      if (currentTournamentData) renderTournament(currentTournamentData);
    });

    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
        // Record it in the URL rather than in storage: a refresh keeps you
        // where you were, back/forward behave sensibly, and a link you send
        // someone opens on the tab you meant. replaceState so switching
        // tabs doesn't stack up history entries you'd have to click back
        // through one at a time.
        history.replaceState(null, '', `#${btn.dataset.tab}`);
        // Finance: if the user is on the allowlist, unlock without ever
        // showing the key box. Only tried when it's still locked.
        if (btn.dataset.tab === 'matches' && typeof defaultMatchGroup === 'function') {
          defaultMatchGroup();
        }
        // The Reviews & Approvals tab is where claim/change requests and
        // finance access now live - load them on open (SuperAdmin only, and
        // the tab itself is hidden for everyone else).
        // The Reviews & Approvals tab: claim/change requests load for anyone
        // who can review (SuperAdmin or a group owner - the backend scopes the
        // results). The rest are SuperAdmin-only controls.
        if (btn.dataset.tab === 'review' && canReviewRequests()) {
          updateReviewTabScope();
          loadClaimRequests();
          if (isSuperAdmin()) {
            loadFinanceAccessList();
            loadUnconfirmedUsers();
            loadClaimAudit();
            loadAppSettings();
            loadEventsAdmin();
            loadStoreAdmin();
            loadQuestsAdmin();
            if (typeof onStoreTypeChange === 'function') onStoreTypeChange();
          }
        }
        if (btn.dataset.tab === 'store') {
          loadStore();
          loadQuests();
        }
        if (btn.dataset.tab === 'finance') {
          // Dues card + ledger selector both read allGroups. If it hasn't
          // finished loading yet (first open / slow network) they'd render
          // empty and "disappear" - so load groups FIRST, then unlock and
          // populate. This is why they only showed up sometimes.
          (async () => {
            if (!allGroups || !allGroups.length) { try { await loadGroups(); } catch (e) {} }
            if (document.getElementById('finance-content').style.display !== 'block') {
              await tryAutoFinanceUnlock();   // populates the ledger selector on success
            } else {
              populateFinanceGroups();
            }
            restoreFinanceMonth();
            populateMyDuesGroups();
          })();
        }
        // Leaving the Player Card has to hand the page back to your own
        // background, so this is re-evaluated on every switch rather than
        // only when a card is rendered.
        updatePageBackground();
      });
    });

    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      document.getElementById('theme-toggle-btn').textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
      localStorage.setItem('networth_theme', theme);
    }

    document.getElementById('display-mode-toggle-btn').textContent = showNicknameFirst ? 'Show: Nickname only' : 'Show: Nickname + Name';
    document.getElementById('display-mode-toggle-btn').addEventListener('click', toggleDisplayMode);

    document.getElementById('theme-toggle-btn').addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      applyTheme(current === 'dark' ? 'light' : 'dark');
    });

    (function initTheme() {
      const saved = localStorage.getItem('networth_theme');
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      applyTheme(saved || (prefersDark ? 'dark' : 'light'));
    })();
