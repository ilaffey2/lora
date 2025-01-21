#!/usr/bin/env python3
import subprocess
import time
from datetime import datetime
import os
import sys
from typing import Dict, List
import curses
import collections
import json
import pathlib

# Store historical channel utilization data
HISTORY_LENGTH = 60  # Keep 60 minutes of history
channel_history = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY_LENGTH))

# Store all seen nodes
class NodeHistory:
    def __init__(self, log_dir=None):
        if log_dir is None:
            log_dir = pathlib.Path.home() / '.tmui'
        self.log_dir = pathlib.Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.history_file = self.log_dir / 'node_history.json'
        self.seen_nodes = self.load_history()
        
    def load_history(self) -> Dict:
        if self.history_file.exists():
            try:
                with open(self.history_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}
        
    def save_history(self):
        with open(self.history_file, 'w') as f:
            json.dump(self.seen_nodes, f, indent=2)
            
    def update_node(self, node: Dict):
        node_id = node['id']
        if node_id not in self.seen_nodes:
            self.seen_nodes[node_id] = {
                'first_seen': datetime.now().isoformat(),
                'times_seen': 1,
                'last_heard': node['last_heard'],
                'latest_info': {}
            }
        else:
            # Only increment times_seen if LastHeard has changed
            if node['last_heard'] != self.seen_nodes[node_id].get('last_heard'):
                self.seen_nodes[node_id]['times_seen'] += 1
                self.seen_nodes[node_id]['last_heard'] = node['last_heard']
        
        self.seen_nodes[node_id]['last_seen'] = datetime.now().isoformat()
        self.seen_nodes[node_id]['latest_info'] = node
        self.save_history()
        
    def get_all_nodes(self) -> List[Dict]:
        return [
            {**info['latest_info'], 
             'first_seen': info['first_seen'],
             'times_seen': info['times_seen']}
            for info in self.seen_nodes.values()
        ]

def parse_table_row(row: str) -> Dict:
    """Parse a single row of the meshtastic table output"""
    # Split on │ and strip whitespace
    cols = [col.strip() for col in row.split('│')]
    if len(cols) < 15:  # Expect at least 15 columns
        return None
        
    try:
        return {
            'user': cols[1],
            'id': cols[2],
            'aka': cols[3],
            'hardware': cols[4],
            'latitude': cols[5],
            'longitude': cols[6],
            'battery': cols[8],
            'channel_util': cols[9],
            'tx_util': cols[10],
            'snr': cols[11],
            'hops': cols[12],
            'last_heard': cols[14],
            'since': cols[15]
        }
    except IndexError:
        return None

def run_meshtastic_command(host: str) -> List[Dict]:
    """Run meshtastic nodes command and parse output"""
    try:
        result = subprocess.run(
            ['meshtastic', '--host', host, '--nodes'],
            capture_output=True,
            text=True,
            check=True
        )
        
        nodes = []
        for line in result.stdout.split('\n'):
            if '│' in line and not any(x in line for x in ['╒', '╕', '╘', '╛', '╞', '╡']):
                node = parse_table_row(line)
                if node:
                    nodes.append(node)
        return nodes
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {e}")
        return []

def update_channel_history(nodes: List[Dict]):
    """Update channel utilization history for each node"""
    timestamp = datetime.now()
    for node in nodes:
        if 'user' in node and 'channel_util' in node:
            try:
                util = float(node['channel_util'].rstrip('%'))
                channel_history[node['user']].append((timestamp, util))
            except (ValueError, AttributeError):
                pass

def draw_graph(stdscr, start_y: int):
    """Draw ASCII graph of channel utilization history"""
    max_width = curses.COLS - 4
    graph_height = 10
    
    # Find max utilization for scaling
    max_util = 1.0  # Minimum scale to 1%
    for history in channel_history.values():
        if history:
            max_util = max(max_util, max(util for _, util in history))
    
    # Draw graph border
    stdscr.addstr(start_y, 0, '┌' + '─' * max_width + '┐')
    for i in range(graph_height):
        stdscr.addstr(start_y + 1 + i, 0, '│' + ' ' * max_width + '│')
    stdscr.addstr(start_y + graph_height + 1, 0, '└' + '─' * max_width + '┘')
    
    # Draw y-axis labels
    stdscr.addstr(start_y + 1, 1, f"{max_util:.1f}%")
    stdscr.addstr(start_y + graph_height, 1, "0%")
    
    # Draw x-axis labels
    minutes_ago = "60m"
    now = "now"
    stdscr.addstr(start_y + graph_height + 1, 1, minutes_ago)
    stdscr.addstr(start_y + graph_height + 1, max_width - len(now) - 1, now)
    
    # Draw data lines
    colors = [1, 2, 3, 4, 5, 6]  # Different colors for different nodes
    for color_idx, (user, history) in enumerate(channel_history.items()):
        if not history:
            continue
            
        color = colors[color_idx % len(colors)]
        stdscr.attron(curses.color_pair(color))
        
        # Plot points
        for i in range(len(history) - 1):
            x1 = int((max_width - 2) * i / (HISTORY_LENGTH - 1))
            x2 = int((max_width - 2) * (i + 1) / (HISTORY_LENGTH - 1))
            y1 = int((graph_height - 1) * (history[i][1] / max_util))
            y2 = int((graph_height - 1) * (history[i + 1][1] / max_util))
            
            # Draw line between points
            if x1 != x2:
                stdscr.addstr(
                    start_y + graph_height - y1,
                    x1 + 1,
                    '─' * (x2 - x1)
                )
        
        # Add legend
        legend_y = start_y + graph_height + 2 + color_idx
        stdscr.addstr(legend_y, 0, f"{user}: ")
        if history:
            stdscr.addstr(f"{history[-1][1]:.1f}%")
            
        stdscr.attroff(curses.color_pair(color))

def draw_history_summary(stdscr, node_history: NodeHistory, start_y: int):
    """Draw summary of historical node data"""
    all_nodes = node_history.get_all_nodes()
    if not all_nodes:
        return
        
    stdscr.addstr(start_y, 0, "Node History:")
    headers = ["User", "AKA", "Times Seen", "First Seen", "Last Seen"]
    for i, header in enumerate(headers):
        stdscr.addstr(start_y + 1, i * 20, f"{header:<19}")
    
    for idx, node in enumerate(all_nodes):
        row = start_y + 2 + idx
        first_seen = datetime.fromisoformat(node['first_seen']).strftime('%Y-%m-%d %H:%M')
        last_seen = datetime.fromisoformat(node.get('last_seen', node['first_seen'])).strftime('%Y-%m-%d %H:%M')
        
        cols = [
            node.get('user', 'N/A'),
            node.get('aka', 'N/A'),
            str(node.get('times_seen', 1)),
            first_seen,
            last_seen
        ]
        for i, col in enumerate(cols):
            stdscr.addstr(row, i * 20, f"{col:<19}")

def main(stdscr, host: str):
    # Setup colors
    curses.start_color()
    curses.use_default_colors()
    for i in range(1, 7):
        curses.init_pair(i, i, -1)
    
    # Hide cursor
    curses.curs_set(0)
    
    # Initialize node history
    node_history = NodeHistory()
    
    while True:
        try:
            # Clear screen
            stdscr.clear()
            
            # Get current nodes data
            nodes = run_meshtastic_command(host)
            
            # Update histories
            update_channel_history(nodes)
            for node in nodes:
                node_history.update_node(node)
            
            # Draw header
            stdscr.addstr(0, 0, f"tmui - Meshtastic Monitor - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            stdscr.addstr(1, 0, "Press 'q' to quit")
            
            # Draw active nodes table
            table_start = 3
            headers = ["User", "AKA", "Hardware", "Battery", "Chan.Util", "SNR", "Since"]
            for i, header in enumerate(headers):
                stdscr.addstr(table_start, i * 15, f"{header:<14}")
            
            for idx, node in enumerate(nodes):
                row = table_start + 1 + idx
                cols = [
                    node.get('user', 'N/A'),
                    node.get('aka', 'N/A'),
                    node.get('hardware', 'N/A'),
                    node.get('battery', 'N/A'),
                    node.get('channel_util', 'N/A'),
                    node.get('snr', 'N/A'),
                    node.get('since', 'N/A')
                ]
                for i, col in enumerate(cols):
                    stdscr.addstr(row, i * 15, f"{col:<14}")
            
            # Draw channel utilization graph
            graph_start = table_start + len(nodes) + 2
            stdscr.addstr(graph_start, 0, "Channel Utilization History (last hour)")
            draw_graph(stdscr, graph_start + 1)
            
            # Draw node history
            history_start = graph_start + 15  # After graph and legends
            draw_history_summary(stdscr, node_history, history_start)
            
            # Refresh display
            stdscr.refresh()
            
            # Check for quit
            stdscr.timeout(1000)  # 1 second timeout on getch
            key = stdscr.getch()
            if key == ord('q'):
                break
                
        except curses.error:
            pass  # Handle terminal resize
        except Exception as e:
            stdscr.addstr(0, 0, f"Error: {str(e)}")
            stdscr.refresh()
            time.sleep(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 tmui.py HOST")
        sys.exit(1)
        
    host = sys.argv[1]
    curses.wrapper(lambda stdscr: main(stdscr, host))
