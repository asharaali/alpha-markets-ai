import { NavLink } from "react-router";
import { SECTIONS } from "../sections";

export function Nav({ base }: { base: string }) {
  return (
    <nav className="nav">
      {SECTIONS.map((s) => (
        <NavLink key={s.path} to={`${base}/${s.path}`} end={s.path === ""}
          className={({ isActive }) => (isActive ? "active" : undefined)}>{s.label}</NavLink>
      ))}
    </nav>
  );
}
