import { Link } from "react-router";
import { useSession } from "../auth";
import { isDemo } from "../lib/api";
import { Nav } from "./Nav";
import { Search } from "./Search";

export function TopBar({ base }: { base: string }) {
  const { user, logout } = useSession();
  return (
    <header className="topbar">
      <Link to={`${base}/`} className="brand">ALPHA MARKETS <span>TERMINAL</span></Link>
      <Nav base={base} />
      <Search base={base} />
      {isDemo() ? <a href="/" className="topbar-user">Sign in</a>
        : <button className="topbar-user" onClick={logout}>{user} · log out</button>}
    </header>
  );
}
