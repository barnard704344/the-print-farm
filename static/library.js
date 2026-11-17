/* Pure library browsing logic, shared by grid and list views. */
window.LibraryBrowser = Object.freeze({
    pageSize: 48,
    fileType(file) {
        const name = String(file.original_name || '').toLowerCase();
        if (name.endsWith('.3mf')) return '3mf';
        if (/\.(?:b?gcode)$/.test(name)) return 'gcode';
        return 'other';
    },
    filter(files, type) {
        return type === 'all' ? files : files.filter(file => this.fileType(file) === type);
    },
    page(files, requested) {
        const pages = Math.max(1, Math.ceil(files.length / this.pageSize));
        const current = Math.min(pages, Math.max(1, requested));
        const start = (current - 1) * this.pageSize;
        return { files: files.slice(start, start + this.pageSize), current, pages, start, total: files.length };
    },
});
