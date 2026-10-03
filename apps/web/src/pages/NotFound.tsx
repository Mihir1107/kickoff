import { ButtonLink } from "@/components/ui/Button";

export function NotFound() {
  return (
    <div className="grid min-h-[60vh] place-items-center text-center">
      <div>
        <div className="font-display text-[120px] leading-none text-gradient">404</div>
        <p className="mt-2 text-white/65">Not found, or not visible to you. The API answers both the same way.</p>
        <ButtonLink to="/" className="mt-6" variant="primary">Back to Command Center</ButtonLink>
      </div>
    </div>
  );
}
