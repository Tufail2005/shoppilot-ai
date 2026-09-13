import React from 'react';
import {
  Search,
  Inbox,
  Package,
  Users,
  BookOpen,
  BarChart2,
  Settings,
  MoreVertical,
  Send,
} from 'lucide-react';

export function DashboardMockup() {
  return (
    <div className="flex h-[500px] w-[740px] overflow-hidden rounded-xl border border-gray-200 bg-[#f9fafc] shadow-2xl text-sm">
      {/* Sidebar */}
      <div className="w-48 border-r border-gray-200 bg-white p-4 flex flex-col gap-6">
        <div className="flex items-center gap-2 font-bold text-gray-900">
          <div className="w-5 h-5 bg-black rounded-md flex items-center justify-center">
            <span className="text-white text-[10px]">S</span>
          </div>
          ShopPilot
        </div>
        
        <nav className="flex flex-col gap-1 text-gray-600 font-medium">
          <div className="flex items-center justify-between bg-gray-100 text-black px-3 py-2 rounded-lg">
            <div className="flex items-center gap-2"><Inbox size={16} /> Inbox</div>
            <span className="bg-black text-white text-[10px] px-1.5 py-0.5 rounded-full">12</span>
          </div>
          <div className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"><Package size={16} /> Orders</div>
          <div className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"><Users size={16} /> Customers</div>
          <div className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"><BookOpen size={16} /> Knowledge</div>
          <div className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"><BarChart2 size={16} /> Analytics</div>
          <div className="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 rounded-lg cursor-pointer"><Settings size={16} /> Settings</div>
        </nav>
      </div>

      {/* Main Content */}
      <div className="flex-1 flex flex-col bg-white">
        {/* Header */}
        <header className="h-14 border-b border-gray-100 flex items-center justify-between px-6">
          <h2 className="font-semibold text-lg">Inbox</h2>
          <div className="flex items-center gap-4">
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input 
                type="text" 
                placeholder="Search conversations..." 
                className="pl-8 pr-4 py-1.5 bg-gray-50 border border-gray-200 rounded-full text-xs w-64 focus:outline-none"
              />
            </div>
            <div className="w-7 h-7 bg-[#a58672] rounded-full flex items-center justify-center text-white text-xs font-medium">JD</div>
          </div>
        </header>

        {/* View Tabs */}
        <div className="flex gap-6 px-6 pt-3 border-b border-gray-100 text-xs font-medium text-gray-500">
          <div className="pb-3 border-b-2 border-black text-black flex items-center gap-1.5">
            All <span className="bg-black text-white px-1.5 py-0.5 rounded-full text-[10px]">13</span>
          </div>
          <div className="pb-3 cursor-pointer">Open <span className="text-gray-400 ml-1">3</span></div>
          <div className="pb-3 cursor-pointer">Resolved</div>
          <div className="pb-3 cursor-pointer">Escalated <span className="text-gray-400 ml-1">2</span></div>
        </div>

        <div className="flex-1 flex overflow-hidden">
          {/* Ticket List */}
          <div className="w-1/3 border-r border-gray-100 overflow-y-auto">
            {/* Active Ticket */}
            <div className="p-4 bg-[#f4f7fa] border-l-2 border-blue-500 cursor-pointer">
              <div className="flex justify-between items-start mb-1">
                <div className="flex items-center gap-2 font-medium text-sm">
                  <div className="w-6 h-6 bg-indigo-200 text-indigo-700 rounded-full flex items-center justify-center text-[10px]">EC</div>
                  Emma Carter
                </div>
                <span className="text-[10px] text-gray-400">2m</span>
              </div>
              <p className="text-xs text-gray-600 truncate ml-8">Where is my order??</p>
            </div>
            {/* Inactive Tickets */}
            {[
              { init: 'LP', name: 'Liam Parker', time: '13m', msg: 'Can I change my shipping address?', color: 'bg-pink-200 text-pink-700' },
              { init: 'CN', name: 'Chloe Nguyen', time: '1h', msg: 'Do you offer international shipping?', color: 'bg-amber-200 text-amber-700' },
              { init: 'NS', name: 'Noah Smith', time: '2h', msg: 'I received a damaged item', color: 'bg-teal-200 text-teal-700' },
              { init: 'AP', name: 'Ava Patel', time: '4h', msg: 'What is your return policy?', color: 'bg-emerald-200 text-emerald-700' },
            ].map((ticket, i) => (
              <div key={i} className="p-4 border-b border-gray-50 hover:bg-gray-50 cursor-pointer transition-colors">
                <div className="flex justify-between items-start mb-1">
                  <div className="flex items-center gap-2 font-medium text-sm">
                    <div className={`w-6 h-6 ${ticket.color} rounded-full flex items-center justify-center text-[10px]`}>{ticket.init}</div>
                    {ticket.name}
                  </div>
                  <span className="text-[10px] text-gray-400">{ticket.time}</span>
                </div>
                <p className="text-xs text-gray-500 truncate ml-8">{ticket.msg}</p>
              </div>
            ))}
          </div>

          {/* Chat Area */}
          <div className="w-2/3 flex flex-col bg-white">
            <div className="p-4 border-b border-gray-100 flex justify-between items-center">
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 bg-indigo-200 text-indigo-700 rounded-full flex items-center justify-center text-xs font-medium">EC</div>
                <div>
                  <h3 className="font-medium text-sm">Emma Carter</h3>
                  <p className="text-xs text-gray-500">emma@example.com</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="bg-gray-100 text-gray-600 text-[10px] px-2 py-1 rounded-md border border-gray-200">Order #4821</span>
                <button className="text-gray-400 hover:text-black"><MoreVertical size={16} /></button>
              </div>
            </div>

            <div className="flex-1 p-6 overflow-y-auto flex flex-col gap-4 bg-[#fafbfc]">
              {/* User Message */}
              <div className="flex gap-3">
                 <div className="w-6 h-6 bg-indigo-200 text-indigo-700 rounded-full flex items-center justify-center text-[10px] shrink-0 mt-1">EC</div>
                 <div>
                   <div className="bg-white border border-gray-100 p-3 rounded-2xl rounded-tl-sm text-sm text-gray-800 shadow-sm">
                     Where is my order??
                   </div>
                   <div className="text-[10px] text-gray-400 mt-1">10:12 AM</div>
                 </div>
              </div>
              
              {/* AI Response */}
              <div className="flex gap-3 self-end flex-row-reverse">
                 <div className="w-6 h-6 bg-black text-white rounded-md flex items-center justify-center text-[10px] shrink-0 mt-1">S</div>
                 <div className="flex flex-col items-end">
                   <div className="bg-[#eff2f9] p-4 rounded-2xl rounded-tr-sm text-sm text-gray-800 max-w-sm">
                     Hi Emma! 👋 <br/><br/>
                     Your order #4821 is currently in transit and is expected to arrive tomorrow (Mar 15). <br/><br/>
                     You can track it here:
                     <button className="block mt-2 bg-white border border-blue-200 text-blue-600 px-3 py-1.5 rounded-lg text-xs font-medium hover:bg-blue-50 transition-colors">
                        Track Order
                     </button>
                   </div>
                   <div className="text-[10px] text-gray-400 mt-1">10:13 AM</div>
                 </div>
              </div>
            </div>

            <div className="p-4 bg-white border-t border-gray-100">
              <div className="relative flex items-center">
                <input 
                  type="text" 
                  placeholder="Type a reply..." 
                  className="w-full pl-4 pr-12 py-3 bg-gray-50 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-1 focus:ring-black focus:border-black"
                />
                <button className="absolute right-2 bg-black text-white p-2 rounded-lg hover:bg-gray-800 transition-colors">
                  <Send size={14} />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}