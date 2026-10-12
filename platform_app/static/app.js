const $ = id => document.getElementById(id);
let state, agent = 'research', busy = false;

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); }
  catch { throw Error('Service unavailable. Please try again.'); }
  if (response.status === 401) {
    location.assign('/login');
    throw Error('Your session expired. Please sign in again.');
  }
  if (!response.ok) {
    const detail = data.detail;
    const error = Error(typeof detail === 'string' ? detail : detail?.message || 'Please check your request.');
    error.status = response.status;
    error.connection = detail?.connection;
    throw error;
  }
  return data;
}

function addMessage(text, role = 'assistant') {
  $('welcome')?.remove();
  const box = document.createElement('div');
  box.className = 'message ' + role;
  const label = document.createElement('small');
  label.textContent = role === 'user' ? 'YOU' : 'AGENT LAB';
  box.append(label, document.createTextNode(text));
  $('messages').append(box);
  box.scrollIntoView({block: 'end'});
  return box;
}

async function connect(id, button) {
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/connections/${encodeURIComponent(id)}/connect`, {method: 'POST'});
    location.assign(result.authorization_url);
  } catch (error) {
    addMessage(error.message).classList.add('error');
    if (button) button.disabled = false;
  }
}

function showConnections() {
  document.querySelectorAll('.connection-prompt').forEach(node => node.remove());
  $('connections').replaceChildren();
  const selected = state.agents.find(item => item.id === agent);
  for (const binding of selected.connections) {
    const connection = state.connections.find(item => item.id === binding.id);
    const row = document.createElement('div');
    row.className = 'connection';
    const name = document.createElement('span');
    name.textContent = connection.name;
    const status = document.createElement('span');
    status.className = 'status';
    status.textContent = connection.connected ? 'Connected' : connection.expired ? 'Reconnect needed' : 'Not connected';
    row.append(name, status);
    $('connections').append(row);
    if (!connection.connected) {
      const prompt = document.createElement('div');
      prompt.className = 'message connection-prompt';
      prompt.textContent = connection.expired ? `${connection.name} needs to be reconnected before continuing.` : `${selected.name} can use ${connection.name.toLowerCase()}. ` +
        (binding.required ? 'Connect to continue.' : 'Connect to include your account and order information. You can also research the web without it.');
      if (connection.managed) {
        prompt.textContent = `${connection.name} needs to be configured in the platform settings.`;
        $('messages').append(prompt);
        continue;
      }
      const button = document.createElement('button');
      button.textContent = (connection.expired ? 'Reconnect ' : 'Connect ') + connection.name;
      button.onclick = () => connect(connection.id, button);
      prompt.append(button);
      $('messages').append(prompt);
    }
  }
  if (!selected.connections.length) $('connections').textContent = 'No connections needed';
  $('tool-hint').textContent = selected.connections.some(binding =>
    state.connections.find(item => item.id === binding.id)?.connected)
    ? 'Connected tools available to this agent' : 'Web research';
}

async function refresh() {
  state = await api('/api/state');
  if (!state.agents.some(item => item.id === agent)) agent = state.agents[0]?.id;
  document.querySelector('.badge').textContent = state.model || 'PLAYGROUND';
  $('agents').replaceChildren();
  state.agents.forEach(item => {
    const button = document.createElement('button');
    button.className = 'agent' + (agent === item.id ? ' selected' : '');
    button.textContent = item.name;
    const description = document.createElement('small');
    description.textContent = item.description;
    button.append(description);
    button.onclick = () => {
      if (busy || agent === item.id) return;
      agent = item.id;
      $('messages').replaceChildren();
      refresh().catch(error => addMessage(error.message));
    };
    $('agents').append(button);
  });
  $('title').textContent = state.agents.find(item => item.id === agent)?.name || 'Agent Lab';
  showConnections();
}

$('chat').onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  const message = $('prompt').value.trim();
  if (message.length < 5) return;
  addMessage(message, 'user');
  $('prompt').value = '';
  busy = true;
  $('send').disabled = true;
  const box = addMessage('Working…');
  let failedConnection;
  try {
    const result = await api('/api/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({agent, message})
    });
    const errors = {
      upstream_http_429: 'The model provider is rate-limiting requests or its quota is exhausted. Try again later or check your provider quota.',
      upstream_http_401: 'The model provider rejected its API key. Check the backend provider configuration.',
      upstream_http_403: 'The model provider denied access. Check model permissions and billing.',
      upstream_http_404: 'The configured model is unavailable. Check the backend model name.',
      upstream_timeout: 'The model provider timed out. Please try again.'
    };
    box.replaceChildren(document.createTextNode(result.answer || errors[result.error_code] ||
      `Run finished: ${result.status || 'no answer'}. ${result.error_code || ''} ${result.limitations?.join(' ') || ''}`));
    if (result.sources?.length) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Sources';
      details.append(summary);
      for (const source of result.sources) {
        try { if (!['https:', 'http:'].includes(new URL(source.url).protocol)) continue; }
        catch { continue; }
        const link = document.createElement('a');
        link.href = source.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = source.title || source.url;
        details.append(link, document.createElement('br'));
      }
      box.append(details);
    }
  } catch (error) {
    box.textContent = error.message;
    box.classList.add('error');
    failedConnection = error.connection;
  } finally {
    busy = false;
    $('send').disabled = false;
    try {
      await refresh();
      // One action per missing service, even when its failure was raised in chat.
      if (failedConnection) {
        const button = document.querySelector('.connection-prompt button');
        if (button) button.textContent = 'Reconnect tools';
      }
    } catch {}
  }
};
$('prompt').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('chat').requestSubmit();
  }
};
document.querySelectorAll('.suggestions button').forEach(button => button.onclick = () => {
  $('prompt').value = button.textContent;
  $('prompt').focus();
});
$('new').onclick = () => {
  if (!busy) {
    $('messages').replaceChildren();
    addMessage('New chat. What would you like to explore?');
    showConnections();
  }
};
refresh().catch(error => addMessage(error.message));
