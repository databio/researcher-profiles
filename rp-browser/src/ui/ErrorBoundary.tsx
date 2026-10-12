/**
 * Last-resort catch for render-time throws. The worst case must be a card
 * with the error and a reload button, never a blank page.
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
