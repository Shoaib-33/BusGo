// ===== Backend Status Check =====
async function checkStatus() {
    const dot = document.getElementById('statusDot');
    if (!dot) return;
    try {
        const res = await fetch('/stats', { signal: AbortSignal.timeout(2000) });
        if (res.ok) {
            dot.classList.add('online');
            dot.classList.remove('offline');
            dot.title = 'Backend connected';
        } else {
            throw new Error();
        }
    } catch {
        dot.classList.add('offline');
        dot.classList.remove('online');
        dot.title = 'Backend offline';
    }
}
checkStatus();

// ===== Auth Status =====
async function checkAuthStatus() {
    const link = document.getElementById('authNavLink');
    const dashboardLink = document.getElementById('dashboardNavLink');
    const adminLink = document.getElementById('adminNavLink');
    if (!link) return;
    try {
        const res = await fetch('/auth/me', { signal: AbortSignal.timeout(2000) });
        const data = await res.json();
        if (data.authenticated && data.user) {
            if (adminLink && data.user.role === 'admin') adminLink.classList.remove('hidden');
            link.textContent = 'Logout';
            link.href = '#';
            link.title = `Logged in as ${data.user.name || data.user.phone}`;
            link.onclick = (event) => {
                event.preventDefault();
                logout();
            };
        }
    } catch {}
}
checkAuthStatus();

async function logout() {
    try {
        await fetch('/auth/logout', {
            method: 'POST',
            credentials: 'same-origin'
        });
        location.href = '/login-page';
    } catch {
        location.href = '/login-page';
    }
}

// ===== Toast Notifications =====
function showToast(message, type = 'success') {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.transition = 'opacity 0.3s';
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}
