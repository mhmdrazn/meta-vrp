import { Outlet, NavLink } from "react-router-dom";
import { ThemeToggle } from "@/components/theme-toggle";
import { Leaf } from "lucide-react";
import { cn } from "@/lib/utils";

function AppLogo({ className }: { className?: string }) {
    return (
        <NavLink
            to="/"
            className={cn(
                "flex items-center gap-2.5 font-bold text-lg tracking-tight",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-md",
                className,
            )}
        >
            <div className="flex items-center justify-center w-8 h-8 bg-green-600 rounded-lg text-white">
                <Leaf className="w-5 h-5" />
            </div>
            <span>Park Watering</span>
        </NavLink>
    );
}

export default function AppShell() {
    return (
        <div className="flex h-screen flex-col overflow-hidden bg-background text-foreground">
            <header className="z-50 border-b border-primary/10 bg-gradient-to-r from-primary/5 via-background to-primary/5 backdrop-blur-xl flex-shrink-0">
                <div className="container mx-auto flex h-14 items-center justify-between px-4">
                    <AppLogo />
                    <ThemeToggle />
                </div>
            </header>

            <main className="flex-1 overflow-y-auto app-scroll">
                <div className="container mx-auto px-4 py-4">
                    <Outlet />
                </div>
            </main>

            <footer className="flex-shrink-0 border-t border-primary/10 bg-gradient-to-r from-transparent via-primary/[0.02] to-transparent">
                <div className="container mx-auto flex h-10 items-center justify-center px-4">
                    <p className="text-xs text-muted-foreground">
                        © {new Date().getFullYear()} MetaVRP Project • Park-Watering
                        Routing Decision-Support System
                    </p>
                </div>
            </footer>
        </div>
    );
}
