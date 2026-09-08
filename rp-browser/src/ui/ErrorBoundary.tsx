/**
 * Last-resort catch for render-time throws.
 *
 * Before this existed, any throw during render unmounted the whole React
 * tree to a literally blank white page. That was indistinguishable from the
 * reported "white-on-white navbar" bug and the reason that diagnosis was
 * ambiguous. A card with the error and a reload button must always be the
 * worst case, never a blank screen.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled error in rp-browser render tree:", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="error-boundary">
        <div className="error-boundary__card">
          <h1 className="error-boundary__heading">Something went wrong</h1>
          <pre className="error-boundary__message">{error.message || String(error)}</pre>
          <button className="btn btn--primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
}
