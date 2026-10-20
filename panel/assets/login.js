const form = document.getElementById('login-form');
const error = document.getElementById('error');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  error.textContent = '';
  const btn = form.querySelector('button');
  btn.disabled = true;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        username: form.username.value,
        password: form.password.value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      location.href = '/admin';
      return;
    }
    error.textContent = data.error || 'Xatolik yuz berdi';
    form.password.value = '';
    form.password.focus();
  } catch (err) {
    error.textContent = 'Server bilan aloqa yo‘q';
  } finally {
    btn.disabled = false;
  }
});
