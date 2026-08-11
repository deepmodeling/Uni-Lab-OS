import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const workflowApiTarget = process.env.VITE_WORKFLOW_API_TARGET ?? 'http://127.0.0.1:8014';
const taskApiTarget = process.env.VITE_TASK_API_TARGET ?? 'http://127.0.0.1:8091';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': workflowApiTarget,
      '/task-api': {
        target: taskApiTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/task-api/, ''),
      },
    },
  },
});
