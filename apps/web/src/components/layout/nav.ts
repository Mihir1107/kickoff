import { Briefcase, FileArchive, Fingerprint, LayoutDashboard, Radar, ShieldCheck } from "lucide-react";

export const NAV = [
  { to: "/", label: "Command Center", icon: LayoutDashboard, end: true },
  { to: "/clients", label: "Clients & Matters", icon: Briefcase },
  { to: "/collections", label: "Collections", icon: Radar },
  { to: "/exports", label: "Slack Exports", icon: FileArchive },
  { to: "/custody", label: "Chain of Custody", icon: Fingerprint },
  { to: "/access", label: "Access Control", icon: ShieldCheck },
] as const;
