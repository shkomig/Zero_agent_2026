import sqlite3
from collections import defaultdict, deque, heapq
import argparse

class Task:
    def __init__(self, name, priority, duration, dependencies=None):
        self.name = name
        self.priority = priority  # 1 (lowest) to 5 (highest)
        self.duration = duration  # in minutes
        self.dependencies = dependencies or []
        self.earliest_start = 0
        self.done = False

    def __repr__(self):
        return f"{self.name} (P{self.priority})"

class TaskManager:
    def __init__(self, db_path='tasks.db'):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE,
                priority INTEGER CHECK(priority BETWEEN 1 AND 5),
                duration INTEGER,
                done BOOLEAN DEFAULT 0
            )''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS dependencies (
                task_id INTEGER,
                dependency_id INTEGER,
                FOREIGN KEY(task_id) REFERENCES tasks(id),
                FOREIGN KEY(dependency_id) REFERENCES tasks(id)
            )''')
            conn.commit()

    def add_task(self, name, priority, duration, dependencies):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO tasks (name, priority, duration) VALUES (?, ?, ?)',
                          (name, priority, duration))
            task_id = cursor.lastrowid

            for dep_name in dependencies:
                cursor.execute('SELECT id FROM tasks WHERE name = ?', (dep_name,))
                dep_id = cursor.fetchone()
                if dep_id:
                    cursor.execute('INSERT INTO dependencies (task_id, dependency_id) VALUES (?, ?)',
                                  (task_id, dep_id[0])
            conn.commit()

    def get_all_tasks(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM tasks')
            rows = cursor.fetchall()

            tasks = []
            for row in rows:
                task_id, name, priority, duration, done = row
                cursor.execute('SELECT name FROM tasks WHERE id IN (SELECT dependency_id FROM dependencies WHERE task_id = ?)', (task_id,))
                deps = [r[0] for r in cursor.fetchall()]
                tasks.append(Task(name, priority, duration, deps))
                tasks[-1].done = bool(done)
                tasks[-1].id = task_id
            return tasks

    def topological_sort(self):
        tasks = self.get_all_tasks()
        name_to_task = {t.name: t for t in tasks}
        in_degree = {t.name: 0 for t in tasks}
        adj = defaultdict(list)

        # Build graph and in-degrees
        for task in tasks:
            for dep in task.dependencies:
                adj[dep].append(task.name)
                in_degree[task.name] += 1

        # Kahn's algorithm
        queue = deque([name for name in in_degree if in_degree[name] == 0])
        result = []

        while queue:
            u = queue.popleft()
            result.append(u)

            for v in adj[u]:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

        if len(result) != len(tasks):
            raise ValueError("Cycle detected in dependencies")
        return [name_to_task[name] for name in result]

    def calculate_earliest_times(self):
        tasks = self.topological_sort()
        name_to_task = {t.name: t for t in tasks}

        # Build dependency graph
        graph = defaultdict(list)
        for task in tasks:
            for dep in task.dependencies:
                graph[dep].append(task.name)

        # Priority queue: (-priority, task name)
        ready = []
        earliest_start = {t.name: 0 for t in tasks}
        
        # Initialize with tasks having no dependencies
        for task in tasks:
            if not task.dependencies:
                heapq.heappush(ready, (-task.priority, task.name))

        while ready:
            priority, task_name = heapq.heappop(ready)
            task = name_to_task[task_name]
            
            # Calculate earliest start based on dependencies
            if task.dependencies:
                dep_end_times = [earliest_start[dep] + name_to_task[dep].duration 
                               for dep in task.dependencies]
                earliest_start[task_name] = max(dep_end_times)
            
            # Process dependents
            for dependent_name in graph[task_name]:
                dependent = name_to_task[dependent_name]
                all_deps_met = all(earliest_start[dep] > 0 for dep in dependent.dependencies)
                if all_deps_met:
                    heapq.heappush(ready, (-dependent.priority, dependent_name))
        
        return earliest_start

    def mark_done(self, task_name):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE tasks SET done = 1 WHERE name = ?', (task_name,))
            conn.commit()

    def list_tasks(self):
        tasks = self.get_all_tasks()
        sorted_tasks = self.topological_sort()
        print("\nTasks in recommended execution order:")
        for task in sorted_tasks:
            status = "✓" if task.done else "✗"
            print(f"{task.name} (P{task.priority}) - {task.duration} min [Status: {status}]")

        print("\nEarliest start times:")
        times = self.calculate_earliest_times()
        for task in sorted_tasks:
            print(f"{task.name}: earliest at {times[task.name]} minutes")


def main():
    parser = argparse.ArgumentParser(description='Task Management System with Dependencies')
    subparsers = parser.add_subparsers(dest='command')

    add_parser = subparsers.add_parser('add')
    add_parser.add_argument('name', help='Task name')
    add_parser.add_argument('priority', type=int, help='Priority (1-5)')
    add_parser.add_argument('duration', type=int, help='Duration in minutes')
    add_parser.add_argument('--depends', nargs='*', default=[], help='Dependencies')

    list_parser = subparsers.add_parser('list')

    done_parser = subparsers.add_parser('done')
    done_parser.add_argument('task', help='Task to mark as done')

    args = parser.parse_args()

    tm = TaskManager()

    if args.command == 'add':
        tm.add_task(args.name, args.priority, args.duration, args.depends)
        print(f"Added task: {args.name} (P{args.priority}, {args.duration} min, depends on {args.depends})")
    elif args.command == 'list':
        tm.list_tasks()
    elif args.command == 'done':
        tm.mark_done(args.task)
        print(f"Marked {args.task} as done")

if __name__ == "__main__":
    main()