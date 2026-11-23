import { useState, type FormEvent } from "react";
import { useSession } from "../auth";

export function LoginScreen() {
  const { login } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true);
    setError(await login(username, password)); setBusy(false);
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <p className="brand">ALPHA MARKETS <span>TERMINAL</span></p>
        <h1>Sign in</h1>
        <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <p className="login-error">{error}</p>}
        <button type="submit" disabled={busy}>Sign in</button>
        <a className="login-demo" href="/demo">View the demo →</a>
      </form>
    </div>
  );
}
